import sys
import time
import argparse

import numpy as np

import torch
from torch import nn, optim

from datasets import MonoTextData

from modules import LSTMEncoder, LSTMDecoder
from modules import VAE


def init_config():
    parser = argparse.ArgumentParser(description='VAE mode collapse study')

    # model hyperparameters
    parser.add_argument('--nz', type=int, default=32, help='latent z size')
    parser.add_argument('--ni', type=int, default=512, help='word embedding size')
    parser.add_argument('--nh', type=int, default=1024, help='LSTM hidden state size')
    parser.add_argument('--dec_dropout_in', type=float, default=0.5, help='LSTM decoder dropout')
    parser.add_argument('--dec_dropout_out', type=float, default=0.5, help='LSTM decoder dropout')

    # optimization parameters
    parser.add_argument('--lr', type=float, default=1.0, help='Learning rate')
    parser.add_argument('--lr_decay', type=float, default=0.5, help='Learning rate')
    parser.add_argument('--clip_grad', type=float, default=5.0, help='')
    parser.add_argument('--optim', type=str, default='adam', help='')
    parser.add_argument('--epochs', type=int, default=40, 
                        help='number of training epochs')
    parser.add_argument('--batch_size', type=int, default=32, help='batch size')


    # KL annealing parameters
    # parser.add_argument('--warm_up', type=int, default=5, help='')
    # parser.add_argument('--kl_start', type=float, default=0.1, help='')

    # data parameters
    parser.add_argument('--train_data', type=str, default='datasets/yahoo/data_yahoo_release/train.txt', 
                        help='training data file')
    parser.add_argument('--test_data', type=str, default='datasets/yahoo/data_yahoo_release/test.txt', 
                        help='testing data file')

    # log parameters
    parser.add_argument('--niter', type=int, default=50, help='report every niter iterations')
    parser.add_argument('--nepoch', type=int, default=1, help='valid every nepoch epochs')

    # select mode
    parser.add_argument('--eval', action='store_true', default=False, help='compute iw nll')
    parser.add_argument('--load_model', type=str, default='')

    # others
    parser.add_argument('--seed', type=int, default=783435, metavar='S', help='random seed')
    parser.add_argument('--save_path', type=str, default='', help='valid every nepoch epochs')


    args = parser.parse_args()
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.cuda:
        torch.cuda.manual_seed(args.seed)

    return args

def test(model, test_data, args):

    report_kl_loss = report_rec_loss = 0
    report_num_words = report_num_sents = 0
    for batch_data, sents_len in test_data.data_iter(batch_size=args.batch_size,
                                                     device=args.device
                                                     batch_first=True):

        batch_size = len(batch_data)

        report_num_sents += batch_size

        # sents_len counts both the start and end symbols
        sents_len = torch.LongTensor(sents_len)

        # not predict start symbol
        report_num_words += (sents_len - 1).sum()


        loss_rc, loss_kl = model.loss((batch_data, sents_len), nsamples=1)

        assert(!loss_rc.requires_grad)
        
        loss_rc = loss_rc.sum()
        loss_kl = loss_kl.sum()


        report_rec_loss += loss_rc.data[0]
        report_kl_loss += loss_kl.data[0]

    test_loss = (report_rec_loss  + report_kl_loss) / report_num_sents

    nll = (report_kl_loss + report_rec_loss) / report_num_sents
    kl = report_kl_loss / report_num_sents
    ppl = np.exp(nll * report_num_sents / report_num_words)

    print('avg_loss: %.4f, kl: %.4f, recon: %.4f, nll: %.4f, ppl: %.4f' % \
           (test_loss, report_kl_loss / report_num_sents, 
            report_rec_loss / report_num_sents, nll, ppl))
    sys.stdout.flush()

    return test_loss, nll, kl, ppl


def main(args):

    def uniform_initializer(stdv):
        def forward(tensor):
            nn.init.uniform(tensor, -stdv, stdv)

        return forward

    def xavier_normal_initializer():
        def forward(tensor):
            nn.init.xavier_normal(tensor)

        return forward

    if args.cuda:
        print('using cuda')

    print('model saving path: %s' % args.save_path)

    print(args)

    schedule = args.epochs / 5
    lr_ = args.lr
    if args.eta > 0 or args.gamma > 0:
        args.kl_start = 1.0

    train_data = MonoTextData(args.train_data)

    vocab = train_data.vocab
    vocab_size = len(vocab)

    test_data = MonoTextData(args.test_data, vocab)

    print('Train data: %d batches' % len(train_data))
    print('finish reading datasets, vocab size is %d' % len(vocab))
    sys.stdout.flush()

    encoder = LSTMEncoder(args, vocab_size, rnn_initializer, emb_initializer)
    decoder = LSTMDecoder(args, vocab, rnn_initializer, emb_initializer)

    device = torch.device("cuda" if args.cuda else "cpu")
    args.device = device
    vae = VAE(encoder, decoder).to(device)

    # if args.eval:
    #     print('begin evaluation')
    #     vae.load_state_dict(torch.load(args.load_model))
    #     vae.eval()
    #     calc_nll(hae, test_data, args)

    #     return 

    if args.optim == 'sgd':
        optimizer = optim.SGD(hae.parameters(), lr=lr_)
    else:
        optimizer = optim.Adam(hae.parameters(), lr=lr_, betas=(0.5, 0.999))

    iter_ = 0
    decay_cnt = 0
    best_loss = 1e4
    best_kl = best_nll = best_ppl = 0
    vae.train()
    start = time.time()

    # kl_weight = args.kl_start
    # anneal_rate = 1.0 / (args.warm_up * (len(train_data) / args.batch_size))

    # calc_nll(hae, test_data, args)

    for epoch in range(args.epochs):
        report_kl_loss = report_rec_loss = 0
        report_num_words = report_num_sents = 0
        for batch_data, sents_len in train_data.data_iter(batch_size=args.batch_size,
                                                          device=device,
                                                          batch_first=True):

            batch_size = len(batch_data)
            # sents_len counts both the start and end symbols
            sents_len = torch.LongTensor(sents_len)

            # not predict start symbol
            report_num_words += (sents_len - 1).sum()

            report_num_sents += batch_size

            optimizer.zero_grad()

            loss_rc, loss_kl = vae.loss((batch_data, sents_len), nsamples=1)
            #print('-----------------')
            #print(loss_bce.mean().data)
            #print(loss_kl.mean().data)
            #print(hlg.mean().data)
            # assert (loss_bce == loss_bce).all()
            # assert (loss_kl == loss_kl).all()
            # assert (hlg == hlg).all()
            loss_rc = loss_rc.sum()
            loss_kl = loss_kl.sum()

            # kl_weight = min(1.0, kl_weight + anneal_rate)
            kl_weight = 1.0

            loss = (loss_rc + kl_weight * loss_kl) / batch_size 

            # assert (loss == loss).all()

            report_rec_loss += loss_rc.data[0]
            report_kl_loss += loss_kl.data[0]

            loss.backward()
            torch.nn.utils.clip_grad_norm(vae.parameters(), args.clip_grad)
            optimizer.step()

            if iter_ % args.niter == 0:
                train_loss = (report_rec_loss  + report_kl_loss) / report_num_sents

                print('epoch: %d, iter: %d, avg_loss: %.4f, kl: %.4f, recon: %.4f,' \
                       'time elapsed %.2fs' %
                       (epoch, iter_, train_loss, report_kl_loss / report_num_sents,
                       report_rec_loss / report_num_sents, time.time() - start))
                sys.stdout.flush()

            iter_ += 1

        if epoch % args.nepoch == 0:
            print('kl weight %.4f' % kl_weight)
            print('epoch: %d, testing' % epoch)
            vae.eval()

            with torch.no_grad():
                loss, nll, kl, ppl = test(hae, test_data, args)

            if loss < best_loss:
                print('update best loss')
                best_loss = loss
                best_nll = nll
                best_kl = kl
                best_ppl = ppl
                torch.vae(hae.state_dict(), args.save_path)

            vae.train()

        if (epoch + 1) % schedule == 0:
            print('update lr, old lr: %f' % lr_)
            lr_ = lr_ * args.lr_decay
            print('new lr: %f' % lr_)
            if args.optim == 'sgd':
                optimizer = optim.SGD(hae.parameters(), lr=lr_)
            else:
                optimizer = optim.Adam(hae.parameters(), lr=lr_, betas=(0.5, 0.999))

    print('best_loss: %.4f, kl: %.4f, nll: %.4f, ppl: %.4f' \
          % (best_loss, best_kl, best_nll, best_ppl))
    sys.stdout.flush()

    # vae.eval()
    # calc_nll(vae, test_data, args)