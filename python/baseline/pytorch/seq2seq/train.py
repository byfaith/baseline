import time
import logging
import torch
import numpy as np
from baseline.progress import create_progress_bar
from baseline.utils import listify, get_model_file, get_metric_cmp
from baseline.train import Trainer, create_trainer, register_trainer, register_training_func
from baseline.pytorch.optz import OptimizerManager
from baseline.bleu import bleu
from baseline.utils import convert_seq2seq_golds, convert_seq2seq_preds

logger = logging.getLogger('baseline')


@register_trainer(task='seq2seq', name='default')
class Seq2SeqTrainerPyTorch(Trainer):

    def __init__(self, model, **kwargs):
        super(Seq2SeqTrainerPyTorch, self).__init__()
        self.gpu = bool(kwargs.get('gpu', True))
        self.clip = float(kwargs.get('clip', 5))
        self.model = model
        self.optimizer = OptimizerManager(self.model, **kwargs)
        self._input = model.make_input
        self._predict = model.predict
        self.crit = model.create_loss()
        self.tgt_rlut = kwargs['tgt_rlut']
        if self.gpu:
            self.model = torch.nn.DataParallel(model).cuda()
            self.crit.cuda()
        self.nsteps = kwargs.get('nsteps', 500)

    @staticmethod
    def _num_toks(tgt_lens):
        return np.sum(tgt_lens)

    def calc_metrics(self, agg, norm):
        metrics = super(Seq2SeqTrainerPyTorch, self).calc_metrics(agg, norm)
        metrics['perplexity'] = np.exp(metrics['avg_loss'])
        return metrics

    def test(self, vs, reporting_fns, phase):
        if phase == 'Test':
            return self._evaluate(vs, reporting_fns)

        self.model.eval()
        total_loss = total_toks = 0
        steps = len(vs)
        self.valid_epochs += 1
        preds = []
        golds = []

        start = time.time()
        pg = create_progress_bar(steps)
        for batch_dict in pg(vs):
            input_ = self._input(batch_dict)
            tgt = input_['tgt']
            tgt_lens = batch_dict['tgt_lengths']
            pred = self.model(input_)
            loss = self.crit(pred, tgt)
            toks = self._num_toks(tgt_lens)
            total_loss += loss.item() * toks
            total_toks += toks
            greedy_preds = [p[0] for p in self._predict(input_, beam=1, make_input=False)]
            preds.extend(convert_seq2seq_preds(greedy_preds, self.tgt_rlut))
            golds.extend(convert_seq2seq_golds(tgt.cpu().numpy(), tgt_lens, self.tgt_rlut))

        metrics = self.calc_metrics(total_loss, total_toks)
        metrics['bleu'] = bleu(preds, golds)[0]
        self.report(
            self.valid_epochs, metrics, start,
            phase, 'EPOCH', reporting_fns
        )
        return metrics


    def _evaluate(self, es, reporting_fns):
        self.model.eval()
        pg = create_progress_bar(len(es))
        preds = []
        golds = []
        start = time.time()
        for batch_dict in pg(es):
            tgt = batch_dict['tgt']
            tgt_lens = batch_dict['tgt_lengths']
            pred = [p[0] for p in self._predict(batch_dict)]
            preds.extend(convert_seq2seq_preds(pred, self.tgt_rlut))
            golds.extend(convert_seq2seq_golds(tgt, tgt_lens, self.tgt_rlut))
        metrics = {'bleu': bleu(preds, golds)[0]}
        self.report(
            0, metrics, start, 'Test', 'EPOCH', reporting_fns
        )
        return metrics


    def train(self, ts, reporting_fns):
        self.model.train()

        epoch_loss = 0
        epoch_toks = 0

        start = time.time()
        self.nstep_start = start
        for batch_dict in ts:

            start_time = time.time()
            self.optimizer.zero_grad()
            input_ = self._input(batch_dict)
            tgt = input_['tgt']
            pred = self.model(input_)
            loss = self.crit(pred, tgt)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.clip)
            self.optimizer.step()
            tgt_lens = batch_dict['tgt_lengths']
            tok_count = self._num_toks(tgt_lens)
            reporting_loss = loss.item() * tok_count
            epoch_loss += reporting_loss
            epoch_toks += tok_count
            self.nstep_agg += reporting_loss
            self.nstep_div += tok_count

            if (self.optimizer.global_step + 1) % self.nsteps == 0:
                metrics = self.calc_metrics(self.nstep_agg, self.nstep_div)
                self.report(
                    self.optimizer.global_step + 1, metrics, self.nstep_start,
                    'Train', 'STEP', reporting_fns, self.nsteps
                )
                self.reset_nstep()

        metrics = self.calc_metrics(epoch_loss, epoch_toks)
        self.train_epochs += 1
        self.report(
            self.train_epochs, metrics, start,
            'Train', 'EPOCH', reporting_fns
        )
        return metrics


@register_training_func('seq2seq')
def fit(model, ts, vs, es=None, **kwargs):

    do_early_stopping = bool(kwargs.get('do_early_stopping', True))
    epochs = int(kwargs.get('epochs', 20))
    model_file = get_model_file('seq2seq', 'pytorch', kwargs.get('basedir'))

    best_metric = 0
    if do_early_stopping:
        early_stopping_metric = kwargs.get('early_stopping_metric', 'bleu')
        early_stopping_cmp, best_metric = get_metric_cmp(early_stopping_metric, kwargs.get('early_stopping_cmp'))
        patience = kwargs.get('patience', epochs)
        logger.info('Doing early stopping on [%s] with patience [%d]', early_stopping_metric, patience)

    reporting_fns = listify(kwargs.get('reporting', []))
    logger.info('reporting %s', reporting_fns)

    after_train_fn = kwargs.get('after_train_fn', None)
    trainer = create_trainer(model, **kwargs)

    last_improved = 0
    for epoch in range(epochs):
        trainer.train(ts, reporting_fns)

        if after_train_fn is not None:
            after_train_fn(model)

        test_metrics = trainer.test(vs, reporting_fns, phase='Valid')

        if do_early_stopping is False:
            model.save(model_file)

        elif early_stopping_cmp(test_metrics[early_stopping_metric], best_metric):
            last_improved = epoch
            best_metric = test_metrics[early_stopping_metric]
            logger.info('New best %.3f', best_metric)
            model.save(model_file)

        elif (epoch - last_improved) > patience:
            logger.info('Stopping due to persistent failures to improve')
            break

    if do_early_stopping is True:
        logger.info('Best performance on %s: %.3f at epoch %d', early_stopping_metric, best_metric, last_improved)

    if es is not None:
        model.load(model_file)
        trainer = Seq2SeqTrainerPyTorch(model, **kwargs)
        test_metrics = trainer.test(es, reporting_fns, phase='Test')
    return test_metrics
