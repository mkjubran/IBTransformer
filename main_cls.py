
from __future__ import print_function

import os
import argparse
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from tqdm import tqdm
from torch.optim.lr_scheduler import CosineAnnealingLR, StepLR
from data import ModelNet40,ModelNet10,ScanObjectNN, MNIST
#from model import PointNet, IBT_cls
from model_3DMNIST import PointNet, IBT_cls
import numpy as np
from torch.utils.data import DataLoader
import time
from thop import profile
from thop import clever_format
from util import cal_loss, IOStream
import sklearn.metrics as metrics
import torchvision.models as models
from ptflops import get_model_complexity_info
from torchstat import stat

import pdb
from fvcore.nn import FlopCountAnalysis
from fvcore.nn import flop_count_table
from fvcore.nn import flop_count_str
import torch.nn.utils.prune as prune



def _init_():
    if not os.path.exists('outputs'):
        os.makedirs('outputs')
    if not os.path.exists('outputs/'+args.exp_name):
        os.makedirs('outputs/'+args.exp_name)
    if not os.path.exists('outputs/'+args.exp_name+'/'+'models'):
        os.makedirs('outputs/'+args.exp_name+'/'+'models')
    os.system('cp main_cls.py outputs'+'/'+args.exp_name+'/'+'main_cls.py.backup')
    os.system('cp model.py outputs' + '/' + args.exp_name + '/' + 'model.py.backup')
    os.system('cp util.py outputs' + '/' + args.exp_name + '/' + 'util.py.backup')
    os.system('cp data.py outputs' + '/' + args.exp_name + '/' + 'data.py.backup')


def train(args, io):
    
    '''
    # for ScanObjectNN dataset
    train_loader = DataLoader(ScanObjectNN(partition='train', num_points=args.num_points), num_workers=8, batch_size=args.batch_size, shuffle=True, drop_last=True)
    test_loader = DataLoader(ScanObjectNN(partition='test', num_points=args.num_points), num_workers=8, batch_size=args.test_batch_size, shuffle=True, drop_last=False)

    '''

    '''
    # for ModelNet40 dataset
    train_loader = DataLoader(ModelNet40(partition='train', num_points=args.num_points), num_workers=8,batch_size=args.batch_size, shuffle=True, drop_last=True)
    test_loader = DataLoader(ModelNet40(partition='test', num_points=args.num_points), num_workers=8,batch_size=args.test_batch_size, shuffle=True, drop_last=False)
    '''


    # for MNIST dataset
    train_loader = DataLoader(MNIST(partition='train', num_points=args.num_points), num_workers=8,batch_size=args.batch_size, shuffle=True, drop_last=True)
    test_loader = DataLoader(MNIST(partition='test', num_points=args.num_points), num_workers=8,batch_size=args.test_batch_size, shuffle=True, drop_last=False)


    device = torch.device("cuda" if args.cuda else "cpu")

    #Try to load models
    if args.model == 'pointnet':
        model = PointNet(args).to(device)
    elif args.model == 'ibt':
        model = IBT_cls(args).to(device)
    else:
        raise Exception("Not implemented")

    #print(str(model))

    model = nn.DataParallel(model)
    # model=model.module.to(device)
    # stat(model,(3,1024))
    print("Let's use", torch.cuda.device_count(), "GPUs!")

    if args.use_sgd:
        print("Use SGD")
        opt = optim.SGD(model.parameters(), lr=args.lr*100, momentum=args.momentum, weight_decay=1e-4)
    else:
        print("Use Adam")
        opt = optim.Adam(model.parameters(), lr=args.lr, weight_decay=1e-4)

    if args.scheduler == 'cos':
        scheduler = CosineAnnealingLR(opt, args.epochs, eta_min=1e-3)
    elif args.scheduler == 'step':
        scheduler = StepLR(opt, step_size=20, gamma=0.7)
    
    criterion = cal_loss

    best_test_acc = 0
    for epoch in range(args.epochs):
        ####################
        # Train
        ####################
        train_loss = 0.0
        count = 0.0
        model.train()
        train_pred = []
        train_true = []
        for i,(data, label) in tqdm(enumerate(train_loader, 0),total=len(train_loader),smoothing=0.9):
            data, label = data.to(device), label.to(device).squeeze()
            data = data.permute(0, 2, 1)
            batch_size = data.size()[0]
            opt.zero_grad()

            #added by jubran to measure number of flops
            '''
            flops = FlopCountAnalysis(model, data)
            #print(flops.total())
            with open('FlopsCount.txt', 'w') as f:
                 f.write(flop_count_table(flops))
                 f.write(flop_count_str(flops))
            '''
            #end by jubran

            # added by jubran for global pruning
            '''
            parameters_to_prune = (
                (model.module.trans0.q_conv, 'weight'),
                (model.module.trans1.q_conv, 'weight'),
            )

            prune.global_unstructured(
                parameters_to_prune,
                pruning_method=prune.L1Unstructured,
                amount=0.5,
            )
            '''
            ### end by jubran

            logits = model(data)
            #pdb.set_trace()
            loss = criterion(logits, label)
            loss.backward()
            opt.step()
            preds = logits.max(dim=1)[1]
            count += batch_size
            train_loss += loss.item() * batch_size
            train_true.append(label.cpu().numpy())
            train_pred.append(preds.detach().cpu().numpy())
        if args.scheduler == 'cos':
            scheduler.step()
        elif args.scheduler == 'step':
            if opt.param_groups[0]['lr'] > 1e-5:
                scheduler.step()
            if opt.param_groups[0]['lr'] < 1e-5:
                for param_group in opt.param_groups:
                    param_group['lr'] = 1e-5

        train_true = np.concatenate(train_true)
        train_pred = np.concatenate(train_pred)
        train_acc = metrics.accuracy_score(train_true, train_pred)
        #pdb.set_trace()
        #avg_per_class_acc,per_acc = metrics.balanced_accuracy_score(train_true, train_pred) # commented by jubran due to error
        avg_per_class_acc = metrics.balanced_accuracy_score(train_true, train_pred)  # Jubran
        outstr = 'Train %d, loss: %.6f, train acc: %.6f, train avg acc: %.6f' % (epoch,
                                                                                 train_loss*1.0/count,
                                                                                 train_acc,
                                                                                 avg_per_class_acc)
        io.cprint(outstr)

        ####################
        # Test
        ####################
        test_loss = 0.0
        count = 0.0
        model.eval()
        test_pred = []
        test_true = []
        with torch.no_grad():
            #for data, label in test_loader:
            for i,(data, label) in tqdm(enumerate(test_loader, 0),total=len(test_loader),smoothing=0.9):
                data, label = data.to(device), label.to(device).squeeze()
                data = data.permute(0, 2, 1)
                batch_size = data.size()[0]

                #added by jubran to measure number of flops
                #flops = FlopCountAnalysis(model, data)
                #print(flops.total())
                #end by jubran

                logits = model(data)
                loss = criterion(logits, label)
                preds = logits.max(dim=1)[1]
                count += batch_size
                test_loss += loss.item() * batch_size
                test_true.append(label.cpu().numpy())
                test_pred.append(preds.detach().cpu().numpy())
            test_true = np.concatenate(test_true)
            test_pred = np.concatenate(test_pred)
            test_acc = metrics.accuracy_score(test_true, test_pred)
            #avg_per_class_acc,per_acc = metrics.balanced_accuracy_score(test_true, test_pred) # Commented by jubran due to an error
            avg_per_class_acc = metrics.balanced_accuracy_score(test_true, test_pred) #Jubran
            outstr = 'Test %d, loss: %.6f, test acc: %.6f, test avg acc: %.6f' % (epoch,
                                                                                test_loss*1.0/count,
                                                                                test_acc,
                                                                                avg_per_class_acc)
            io.cprint(outstr)
            if test_acc >= best_test_acc:
                best_test_acc = test_acc
                torch.save(model.state_dict(), 'outputs/%s/models/model.t7' % args.exp_name)


def test(args, io):
    
    test_loader = DataLoader(ScanObjectNN(partition='test', num_points=args.num_points),
                             batch_size=args.test_batch_size, shuffle=False, drop_last=False)

    device = torch.device("cuda" if args.cuda else "cpu")
    
    #Try to load models
    if args.model == 'pointnet':
        model = PointNet(args).to(device)
    elif args.model == 'ibt':
        model = IBT_cls(args).to(device)
    else:
        raise Exception("Not implemented")

    model = nn.DataParallel(model)
    
    model.load_state_dict(torch.load(args.model_path))
    # model = model.module.to(device)
    model = model.eval()
    # stat(model, (3, 1024))
    test_acc = 0.0
    count = 0.0
    test_true = []
    test_pred = []
    for data, label in test_loader:
        data, label = data.to(device), label.to(device).squeeze()
        data = data.permute(0, 2, 1)
        batch_size = data.size()[0]
        logits = model(data)
        preds = logits.max(dim=1)[1]
        test_true.append(label.cpu().numpy())
        test_pred.append(preds.detach().cpu().numpy())
        
    test_true = np.concatenate(test_true)
    test_pred = np.concatenate(test_pred)
    test_acc = metrics.accuracy_score(test_true, test_pred)
    avg_per_class_acc , per_acc = metrics.balanced_accuracy_score(test_true, test_pred)
    outstr = 'Test :: test acc: %.6f, test avg acc: %.6f'%(test_acc, avg_per_class_acc)
    io.cprint(outstr)


if __name__ == "__main__":
    # Training settings
    parser = argparse.ArgumentParser(description='Point Cloud Recognition')
    parser.add_argument('--exp_name', type=str, default='exp', metavar='N',help='Name of the experiment')
    parser.add_argument('--model', type=str, default='ibt', metavar='N',choices=['pointnet', 'ibt'],
                        help='Model to use, [pointnet, ibt]')
    parser.add_argument('--dataset', type=str, default='modelnet40', metavar='N',choices=['modelnet40'])
    parser.add_argument('--batch_size', type=int, default=24, metavar='batch_size',help='Size of batch)')
    parser.add_argument('--test_batch_size', type=int, default=12, metavar='batch_size', help='Size of batch)')
    parser.add_argument('--epochs', type=int, default=200, metavar='N',help='number of episode to train ')
    parser.add_argument('--use_sgd', type=bool, default=True,help='Use SGD')
    parser.add_argument('--lr', type=float, default=0.001, metavar='LR',help='learning rate (default: 0.001, 0.1 if using sgd)')
    parser.add_argument('--momentum', type=float, default=0.9, metavar='M',help='SGD momentum (default: 0.9)')
    parser.add_argument('--scheduler', type=str, default='cos', metavar='N',choices=['cos', 'step'],
                        help='Scheduler to use, [cos, step]')
    parser.add_argument('--no_cuda', type=bool, default=False,help='enables CUDA training')
    parser.add_argument('--seed', type=int, default=1, metavar='S',help='random seed (default: 1)')
    parser.add_argument('--eval', type=bool,  default=False, help='evaluate the model')
    parser.add_argument('--num_points', type=int, default=1024,help='num of points to use')
    parser.add_argument('--dropout', type=float, default=0.5,help='initial dropout rate')
    parser.add_argument('--emb_dims', type=int, default=1024, metavar='N', help='Dimension of embeddings')
    parser.add_argument('--k', type=int, default=40, metavar='N',help='Num of nearest neighbors to use')
    parser.add_argument('--model_path', type=str, default='', metavar='N',help='Pretrained model path')
    args = parser.parse_args()

    _init_()

    io = IOStream('outputs/' + args.exp_name + '/run.log')
    io.cprint(str(args))

    args.cuda = not args.no_cuda and torch.cuda.is_available()
    torch.manual_seed(args.seed)
    if args.cuda:
        io.cprint(
            'Using GPU : ' + str(torch.cuda.current_device()) + ' from ' + str(torch.cuda.device_count()) + ' devices')
        torch.cuda.manual_seed(args.seed)
    else:
        io.cprint('Using CPU')

    if not args.eval:
        train(args, io)
    else:
        test(args, io)
