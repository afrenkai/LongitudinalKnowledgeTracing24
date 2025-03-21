# coding: utf-8
# ORIGINAL AUTHOR 2021/4/23 @ zengxiaonan

import logging
import gc
import numpy as np
import torch
from tqdm import tqdm
import os
from torch import nn
from torch.utils.data import DataLoader
from sklearn.metrics import roc_auc_score, log_loss, f1_score

class Net(nn.Module):
    def __init__(self, num_questions, hidden_size, num_layers, dropout_rate):
        super(Net, self).__init__()
        self.hidden_dim = hidden_size
        self.layer_dim = num_layers
        self.lstm = nn.LSTM(num_questions * 2, hidden_size, num_layers, batch_first=True)
        self.dropout = nn.Dropout(dropout_rate)
        self.fc = nn.Linear(self.hidden_dim, num_questions)

    def forward(self, x):
        lstm_out, _ = self.lstm(x)
        out = self.dropout(lstm_out)
        res = torch.sigmoid(self.fc(out))
        return res


def process_raw_pred(raw_question_matrix, raw_pred, num_questions: int) -> tuple:
    questions = torch.nonzero(raw_question_matrix)[1:, 1] % num_questions
    length = questions.shape[0]
    pred = raw_pred[: length]
    pred = pred.gather(1, questions.view(-1, 1)).flatten()
    truth = torch.nonzero(raw_question_matrix)[1:, 1] // num_questions
    del questions
    return pred, truth


class DKT:
    def __init__(self, batch_size=64, num_steps=50, hidden_size=128, lr=1e-4, dropout_rate=0.15, reg_lambda = 1e-3, gpu_num=0, patience=5):
        self.vocab = []
        self.vocab_size = 0
        self.enc_dict = {}
        self.num_steps = num_steps
        self.hidden_size = hidden_size
        self.num_layers = 1
        self.lr = lr
        self.batch_size = batch_size
        self.reg_lambda = reg_lambda
        self.dropout_rate = dropout_rate
        self.dkt_model = None
        self.patience = patience
        os.environ['CUDA_VISIBLE_DEVICES'] = str(gpu_num)
        os.environ['PYTORCH_CUDA_ALLOC_CONF'] = 'expandable_segments:True'
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    def preprocess(self, df, fitting=False):
        if fitting:
            self.vocab = df['skill_id'].unique().tolist()
            self.vocab_size = len(self.vocab) + 1
            self.enc_dict = {sk_id: i for i, sk_id in enumerate(self.vocab, start=1)}

        df.drop_duplicates('problem_log_id', inplace=True)
        df.sort_values(by=['user_xid','start_time'], inplace=True)

        seqs = []
        for name, group in tqdm(df.groupby(by='user_xid')):
            group_len = group.shape[0]
            mod = 0 if group_len % self.num_steps == 0 else (self.num_steps - group_len % self.num_steps)
            oh = np.zeros(shape=(group_len + mod, self.vocab_size * 2))
            i = 0
            for idx, row in group.iterrows():
                skill = row['skill_id']
                corr = row['discrete_score']
                found_vocab = self.check_vocab(skill)
                col_idx = found_vocab if corr == 0 else found_vocab + self.vocab_size
                oh[i][col_idx] = 1
                i += 1
            seqs.append(oh)

        seqs = np.concatenate(seqs)
        full_data = torch.FloatTensor(seqs.reshape(-1, self.num_steps, 2 * self.vocab_size))
        d_l = DataLoader(full_data, batch_size=self.batch_size)
        return d_l

    def fit(self, train_data, num_epochs) -> ...:
        # print(torch.cuda.is_available())
        train_data = self.preprocess(train_data, fitting=True)
        self.dkt_model = Net(self.vocab_size, self.hidden_size, self.num_layers, self.dropout_rate)
        self.dkt_model.to(self.device)
        loss_function = nn.BCELoss()
        optimizer = torch.optim.Adam(self.dkt_model.parameters(), lr=self.lr)

        best_loss = 10
        pat_count = 0
        for e in range(num_epochs):
            all_pred, all_target = [], []

            i = 0
            for batch in tqdm(train_data, "Epoch %s" % e):
                try:
                    batch = batch.to(self.device)
                    integrated_pred = self.dkt_model(batch)
                    batch_size = batch.shape[0]
                    for student in range(batch_size):
                        pred, truth = process_raw_pred(batch[student], integrated_pred[student], self.vocab_size)
                        all_pred.append(pred.to('cpu'))
                        all_target.append(truth.to('cpu').float())
                        del pred, truth
                    del batch, integrated_pred
                    if i > 100:
                        gc.collect()
                        i = 0
                except RuntimeError as e:
                    if "out of memory" in str(e):
                        optimizer.zero_grad()
                        gc.collect()
                        torch.cuda.empty_cache()
                        continue


            all_pred = torch.cat(all_pred)
            all_target = torch.cat(all_target)
            try:
                loss = loss_function(all_pred.to(self.device), all_target.to(self.device))
            except RuntimeError:
                torch.cuda.empty_cache()
                loss = loss_function(all_pred.to(self.device), all_target.to(self.device))
            # back propagation
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            if best_loss < loss:
                pat_count += 1
                if pat_count == self.patience:
                    print(f'Minimal improvement for {self.patience} epochs, ending training')
                    break
            else:
                pat_count = 0
                best_loss = loss

            print("[Epoch %d] LogisticLoss: %.6f" % (e, loss))

        return roc_auc_score(all_target.detach().numpy(), all_pred.detach().numpy())

    def evaluate(self, test_data) -> float:
        test_data = self.preprocess(test_data)
        self.dkt_model.eval()
        y_pred = []
        y_truth = []
        for batch in tqdm(test_data, "evaluating"):
            batch = batch.to(self.device)
            integrated_pred = self.dkt_model(batch)
            batch_size = batch.shape[0]
            for student in range(batch_size):
                pred, truth = process_raw_pred(batch[student], integrated_pred[student], self.vocab_size)
                y_pred.append(pred.to('cpu'))
                y_truth.append(truth.to('cpu').float())
                del pred, truth
            del batch, integrated_pred

        y_pred = torch.cat(y_pred)
        y_truth = torch.cat(y_truth)
        y_truth = y_truth.detach().numpy()
        y_pred = y_pred.detach().numpy()
        y_pred_class = np.round(y_pred)

        auc = roc_auc_score(y_truth,y_pred)
        ll = log_loss(y_truth, y_pred)
        f1 = f1_score(y_truth, y_pred_class)

        return auc, ll, f1

    def check_vocab(self, key):
        return self.enc_dict.get(key, 0)

    def save(self, filepath):
        torch.save(self.dkt_model.state_dict(), filepath)
        logging.info("save parameters to %s" % filepath)

    def load(self, filepath):
        self.dkt_model.load_state_dict(torch.load(filepath))
        logging.info("load parameters from %s" % filepath)