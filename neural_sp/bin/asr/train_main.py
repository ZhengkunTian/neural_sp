#! /usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright 2018 Kyoto University (Hirofumi Inaguma)
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)

"""Train the ASR model."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
# from setproctitle import setproctitle
from tensorboardX import SummaryWriter
import time
import torch
from tqdm import tqdm

from neural_sp.bin.asr.train_utils import Controller
from neural_sp.bin.asr.train_utils import Reporter
from neural_sp.bin.asr.train_utils import Updater
from neural_sp.datasets.loader_asr import Dataset
from neural_sp.evaluators.character import eval_char
from neural_sp.evaluators.phone import eval_phone
from neural_sp.evaluators.word import eval_word
from neural_sp.evaluators.wordpiece import eval_wordpiece
from neural_sp.models.data_parallel import CustomDataParallel
from neural_sp.models.seq2seq.seq2seq import Seq2seq
from neural_sp.models.rnnlm.rnnlm import RNNLM
from neural_sp.utils.config import save_config
from neural_sp.utils.general import mkdir_join
from neural_sp.utils.general import set_logger

torch.manual_seed(1623)
torch.cuda.manual_seed_all(1623)


decode_params = {
    'batch_size': 1,
    'beam_width': 1,
    'min_len_ratio': 0,
    'max_len_ratio': 1,
    'len_penalty': 0,
    'cov_penalty': 0,
    'cov_threshold': 0,
    'rnnlm_weight': 0,
    'resolving_unk': False,
}


def train(args):

    # Automatically reduce batch size in multi-GPU setting
    if args.ngpus > 1:
        args.batch_size -= 10
        args.print_step //= args.ngpus

    subsample_factor = 1
    subsample_factor_sub = 1
    for p in args.conv_poolings:
        if len(p) > 0:
            subsample_factor *= p[0]
    if args.train_set_sub is not None:
        subsample_factor_sub = subsample_factor * (2**sum(args.subsample[:args.enc_num_layers_sub - 1]))
    subsample_factor *= 2**sum(args.subsample)

    # Load dataset
    train_set = Dataset(corpus=args.corpus,
                        csv_path=args.train_set,
                        dict_path=args.dict,
                        label_type=args.label_type,
                        batch_size=args.batch_size * args.ngpus,
                        max_epoch=args.num_epochs,
                        max_num_frames=args.max_num_frames,
                        min_num_frames=args.min_num_frames,
                        sort_by_input_length=True,
                        short2long=True,
                        sort_stop_epoch=args.sort_stop_epoch,
                        dynamic_batching=True,
                        use_ctc=args.ctc_weight > 0,
                        subsample_factor=subsample_factor,
                        csv_path_sub=args.train_set_sub,
                        dict_path_sub=args.dict_sub,
                        label_type_sub=args.label_type_sub,
                        use_ctc_sub=args.ctc_weight_sub > 0,
                        subsample_factor_sub=subsample_factor_sub)
    dev_set = Dataset(corpus=args.corpus,
                      csv_path=args.dev_set,
                      dict_path=args.dict,
                      label_type=args.label_type,
                      batch_size=args.batch_size * args.ngpus,
                      max_epoch=args.num_epochs,
                      max_num_frames=args.max_num_frames,
                      min_num_frames=args.min_num_frames,
                      shuffle=True,
                      use_ctc=args.ctc_weight > 0,
                      subsample_factor=subsample_factor,
                      csv_path_sub=args.dev_set_sub,
                      dict_path_sub=args.dict_sub,
                      label_type_sub=args.label_type_sub,
                      use_ctc_sub=args.ctc_weight_sub > 0,
                      subsample_factor_sub=subsample_factor_sub)
    eval_sets = []
    for set in args.eval_sets:
        # swbd etc.
        if 'phone' in args.label_type and args.corpus != 'timit':
            continue
        eval_sets += [Dataset(corpus=args.corpus,
                              csv_path=set,
                              dict_path=args.dict,
                              label_type=args.label_type,
                              batch_size=1,
                              max_epoch=args.num_epochs,
                              is_test=True)]

    args.num_classes = train_set.num_classes
    args.input_dim = train_set.input_dim
    args.num_classes_sub = train_set.num_classes_sub

    # Load a RNNLM config file for cold fusion & RNNLM initialization
    # if config['rnnlm_path_cold_fusion']:
    #     if args.model is not None:
    #         config['rnnlm_config_cold_fusion'] = load_config(
    #             os.path.join(config['rnnlm_path_cold_fusion'], 'config.yml'), is_eval=True)
    #     elif args.saved_model is not None:
    #         config = load_config(os.path.join(
    #             args.saved_model, 'config_rnnlm_cf.yml'))
    #     assert args.label_type == config['rnnlm_config_cold_fusion']['label_type']
    #     config['rnnlm_config_cold_fusion']['num_classes'] = train_set.num_classes
    args.rnnlm_cf = None
    args.rnnlm_init = None

    # Model setting
    model = Seq2seq(args)
    model.name = args.enc_type
    if len(args.conv_channels) > 0:
        tmp = model.name
        model.name = 'conv' + str(len(args.conv_channels)) + 'L'
        if args.conv_batch_norm:
            model.name += 'bn'
        model.name += tmp
    model.name += str(args.enc_num_units) + 'H'
    model.name += str(args.enc_num_projs) + 'P'
    model.name += str(args.enc_num_layers) + 'L'
    model.name += '_subsample' + str(subsample_factor)
    model.name += '_' + args.dec_type
    model.name += str(args.dec_num_units) + 'H'
    # model.name += str(args.dec_num_projs) + 'P'
    model.name += str(args.dec_num_layers) + 'L'
    model.name += '_' + args.att_type
    if args.att_num_heads > 1:
        model.name += '_head' + str(args.att_num_heads)
    model.name += '_' + args.optimizer
    model.name += '_lr' + str(args.learning_rate)
    model.name += '_bs' + str(args.batch_size)
    model.name += '_ss' + str(args.ss_prob)
    model.name += '_ls' + str(args.lsm_prob)
    if args.ctc_weight > 0:
        model.name += '_ctc' + str(args.ctc_weight)
    if args.bwd_weight > 0:
        model.name += '_bwd' + str(args.bwd_weight)
    if args.main_task_weight < 1:
        model.name += '_main' + str(args.main_task_weight)
        if args.ctc_weight_sub > 0:
            model.name += '_ctcsub' + str(args.ctc_weight_sub * (1 - args.main_task_weight))
        else:
            model.name += '_attsub' + str(1 - args.main_task_weight)

    if args.saved_model is None:
        # Load pre-trained RNNLM
        # if config['rnnlm_path_cold_fusion']:
        #     rnnlm = RNNLM(args)
        #     rnnlm.load_checkpoint(save_path=config['rnnlm_path_cold_fusion'], epoch=-1)
        #     rnnlm.flatten_parameters()
        #
        #     # Fix RNNLM parameters
        #     for param in rnnlm.parameters():
        #         param.requires_grad = False
        #
        #     # Set pre-trained parameters
        #     if config['rnnlm_config_cold_fusion']['backward']:
        #         model.dec_0_bwd.rnnlm = rnnlm
        #     else:
        #         model.dec_0_fwd.rnnlm = rnnlm
        # TODO: 最初にRNNLMのモデルをコピー

        # Set save path
        save_path = mkdir_join(args.model, args.model_type,
                               '_'.join(os.path.basename(args.train_set).split('.')[:-1]), model.name)
        model.set_save_path(save_path)  # avoid overwriting

        # Save the config file as a yaml file
        save_config(vars(args), model.save_path)

        # Setting for logging
        logger = set_logger(os.path.join(model.save_path, 'train.log'), key='training')

        for k, v in sorted(vars(args).items(), key=lambda x: x[0]):
            logger.info('%s: %s' % (k, str(v)))

        # if os.path.isdir(args.pretrained_model):
        #     # NOTE: Start training from the pre-trained model
        #     # This is defferent from resuming training
        #     model.load_checkpoint(args.pretrained_model, epoch=-1,
        #                           load_pretrained_model=True)

        # Count total parameters
        for name in sorted(list(model.num_params_dict.keys())):
            num_params = model.num_params_dict[name]
            logger.info("%s %d" % (name, num_params))
        logger.info("Total %.2f M parameters" % (model.total_parameters / 1000000))

        # Set optimizer
        model.set_optimizer(optimizer=args.optimizer,
                            learning_rate_init=float(args.learning_rate),
                            weight_decay=float(args.weight_decay),
                            clip_grad_norm=args.clip_grad_norm,
                            lr_schedule=False,
                            factor=args.decay_rate,
                            patience_epoch=args.decay_patient_epoch)

        epoch, step = 1, 0
        learning_rate = float(args.learning_rate)
        metric_dev_best = 100

    # NOTE: Restart from the last checkpoint
    # elif args.saved_model is not None:
    #     # Set save path
    #     model.save_path = args.saved_model
    #
    #     # Setting for logging
    #     logger = set_logger(os.path.join(model.save_path, 'train.log'), key='training')
    #
    #     # Set optimizer
    #     model.set_optimizer(
    #         optimizer=config['optimizer'],
    #         learning_rate_init=float(config['learning_rate']),  # on-the-fly
    #         weight_decay=float(config['weight_decay']),
    #         clip_grad_norm=config['clip_grad_norm'],
    #         lr_schedule=False,
    #         factor=config['decay_rate'],
    #         patience_epoch=config['decay_patient_epoch'])
    #
    #     # Restore the last saved model
    #     epoch, step, learning_rate, metric_dev_best = model.load_checkpoint(
    #         save_path=args.saved_model, epoch=-1, restart=True)
    #
    #     if epoch >= config['convert_to_sgd_epoch']:
    #         model.set_optimizer(
    #             optimizer='sgd',
    #             learning_rate_init=float(config['learning_rate']),  # on-the-fly
    #             weight_decay=float(config['weight_decay']),
    #             clip_grad_norm=config['clip_grad_norm'],
    #             lr_schedule=False,
    #             factor=config['decay_rate'],
    #             patience_epoch=config['decay_patient_epoch'])
    #
    #     if config['rnnlm_path_cold_fusion']:
    #         if config['rnnlm_config_cold_fusion']['backward']:
    #             model.rnnlm_0_bwd.flatten_parameters()
    #         else:
    #             model.rnnlm_0_fwd.flatten_parameters()

    train_set.epoch = epoch - 1  # start from index:0

    # GPU setting
    if args.ngpus >= 1:
        model = CustomDataParallel(model, device_ids=list(range(0, args.ngpus, 1)), benchmark=True)
        model.cuda()

    logger.info('PID: %s' % os.getpid())
    logger.info('USERNAME: %s' % os.uname()[1])

    # Set process name
    title = args.corpus + '_' + args.label_type
    # setproctitle(title)

    # Set learning rate controller
    lr_controller = Controller(learning_rate_init=learning_rate,
                               decay_type=args.decay_type,
                               decay_start_epoch=args.decay_start_epoch,
                               decay_rate=args.decay_rate,
                               decay_patient_epoch=args.decay_patient_epoch,
                               lower_better=True,
                               best_value=metric_dev_best)

    # Set reporter
    reporter = Reporter(model.module.save_path, max_loss=300)

    # Set the updater
    updater = Updater(args.clip_grad_norm)

    # Setting for tensorboard
    tf_writer = SummaryWriter(model.module.save_path)

    start_time_train = time.time()
    start_time_epoch = time.time()
    start_time_step = time.time()
    not_improved_epoch = 0
    loss_train_mean, acc_train_mean = 0, 0
    pbar_epoch = tqdm(total=len(train_set))
    pbar_all = tqdm(total=len(train_set) * args.num_epochs)
    while True:
        # Compute loss in the training set (including parameter update)
        batch_train, is_new_epoch = train_set.next()
        model, loss_train, acc_train = updater(model, batch_train)
        loss_train_mean += loss_train
        acc_train_mean += acc_train
        pbar_epoch.update(len(batch_train['xs']))

        if (step + 1) % args.print_step == 0:
            # Compute loss in the dev set
            batch_dev = dev_set.next()[0]
            model, loss_dev, acc_dev = updater(model, batch_dev, is_eval=True)

            loss_train_mean /= args.print_step
            acc_train_mean /= args.print_step
            reporter.step(step, loss_train_mean, loss_dev, acc_train_mean, acc_dev)

            # Logging by tensorboard
            tf_writer.add_scalar('train/loss', loss_train_mean, step + 1)
            tf_writer.add_scalar('dev/loss', loss_dev, step + 1)
            # for n, p in model.module.named_parameters():
            #     n = n.replace('.', '/')
            #     if p.grad is not None:
            #         tf_writer.add_histogram(n, p.data.cpu().numpy(), step + 1)
            #         tf_writer.add_histogram(n + '/grad', p.grad.data.cpu().numpy(), step + 1)

            duration_step = time.time() - start_time_step
            logger.info("...Step:%d(epoch:%.2f) loss:%.2f(%.2f)/acc:%.2f(%.2f)/lr:%.5f/bs:%d/x_len:%d (%.2f min)" %
                        (step + 1, train_set.epoch_detail,
                         loss_train_mean, loss_dev, acc_train_mean, acc_dev,
                         learning_rate, train_set.current_batch_size,
                         max(len(x) for x in batch_train['xs']),
                         duration_step / 60))
            start_time_step = time.time()
            loss_train_mean, acc_train_mean = 0, 0
        step += args.ngpus

        # Save checkpoint and evaluate model per epoch
        if is_new_epoch:
            duration_epoch = time.time() - start_time_epoch
            logger.info('===== EPOCH:%d (%.2f min) =====' % (epoch, duration_epoch / 60))

            # Save fugures of loss and accuracy
            reporter.epoch()

            if epoch < args.eval_start_epoch:
                # Save the model
                model.module.save_checkpoint(model.module.save_path, epoch, step,
                                             learning_rate, metric_dev_best)
            else:
                start_time_eval = time.time()
                # dev
                if args.label_type == 'word':
                    metric_dev = eval_word([model.module], dev_set, decode_params)[0]
                    logger.info('  WER (%s): %.3f %%' % (dev_set.set, metric_dev))
                elif args.label_type == 'wordpiece':
                    metric_dev = eval_wordpiece([model.module], dev_set, decode_params,
                                                args.wp_model)[0]
                    logger.info('  WER (%s): %.3f %%' % (dev_set.set, metric_dev))
                elif 'char' in args.label_type:
                    metric_dev = eval_char([model.module], dev_set, decode_params)[1][0]
                    logger.info('  CER (%s): %.3f %%' % (dev_set.set, metric_dev))
                elif 'phone' in args.label_type:
                    metric_dev = eval_phone([model.module], dev_set, decode_params)[0]
                    logger.info('  PER (%s): %.3f %%' % (dev_set.set, metric_dev))
                else:
                    raise ValueError(args.label_type)

                if metric_dev < metric_dev_best:
                    metric_dev_best = metric_dev
                    not_improved_epoch = 0
                    logger.info('||||| Best Score |||||')

                    # Update learning rate
                    model.module.optimizer, learning_rate = lr_controller.decay_lr(
                        optimizer=model.module.optimizer,
                        learning_rate=learning_rate,
                        epoch=epoch,
                        value=metric_dev)

                    # Save the model
                    model.module.save_checkpoint(model.module.save_path, epoch, step,
                                                 learning_rate, metric_dev_best)

                    # test
                    for eval_set in eval_sets:
                        if args.label_type == 'word':
                            wer_test = eval_word([model.module], eval_set, decode_params)[0]
                            logger.info('  WER (%s): %.3f %%' % (eval_set.set, wer_test))
                        elif args.label_type == 'wordpiece':
                            wer_test = eval_wordpiece([model.module], eval_set, decode_params)[0]
                            logger.info('  WER (%s): %.3f %%' % (eval_set.set, wer_test))
                        elif 'char' in args.label_type:
                            cer_test = eval_char([model.module], eval_set, decode_params)[1][0]
                            logger.info('  CER (%s): %.3f / %.3f %%' % (eval_set.set, cer_test))
                        elif 'phone' in args.label_type:
                            per_test = eval_phone([model.module], eval_set, decode_params)[0]
                            logger.info('  PER (%s): %.3f %%' % (eval_set.set, per_test))
                        else:
                            raise ValueError(args.label_type)
                else:
                    # Update learning rate
                    model.module.optimizer, learning_rate = lr_controller.decay_lr(
                        optimizer=model.module.optimizer,
                        learning_rate=learning_rate,
                        epoch=epoch,
                        value=metric_dev)

                    not_improved_epoch += 1

                duration_eval = time.time() - start_time_eval
                logger.info('Evaluation time: %.2f min' % (duration_eval / 60))

                # Early stopping
                if not_improved_epoch == args.not_improved_patient_epoch:
                    break

                if epoch == args.convert_to_sgd_epoch:
                    # Convert to fine-tuning stage
                    model.module.set_optimizer(
                        'sgd',
                        learning_rate_init=args.learning_rate,
                        weight_decay=float(args.weight_decay),
                        clip_grad_norm=args.clip_grad_norm,
                        lr_schedule=False,
                        factor=args.decay_rate,
                        patience_epoch=args.decay_patient_epoch)
                    logger.info('========== Convert to SGD ==========')

            pbar_epoch = tqdm(total=len(train_set))
            pbar_all.update(len(train_set))

            if epoch == args.num_epochs:
                break

            for eval_set in eval_sets:
                eval_set.epoch += 1
            start_time_step = time.time()
            start_time_epoch = time.time()
            epoch += 1

    duration_train = time.time() - start_time_train
    logger.info('Total time: %.2f hour' % (duration_train / 3600))

    tf_writer.close()
    pbar_epoch.close()
    pbar_all.close()

    return model.module.save_path
