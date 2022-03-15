import pytorch_lightning as pl
from transformers import T5ForConditionalGeneration, Adafactor, T5Tokenizer, AutoModelForSeq2SeqLM, AutoTokenizer
from torch.utils.data import DataLoader, random_split
from .utils import *
from code import dataloading as dl
import warnings
import datasets
import torch
import numpy as np
from sklearn import metrics as metrics

# set seeds
pl.seed_everything(np.random.randint(0, 1000))
# just here for cleaner console output
warnings.filterwarnings("ignore")

# text quality metrics
sacrebleu = datasets.load_metric('sacrebleu')
rouge = datasets.load_metric('rouge')
meteor = datasets.load_metric('meteor')


# pytorch-lightning module to fine-tune model on scores
class LitSAFT5(pl.LightningModule):
    def __init__(self, batch_size, with_questions=True, label='score', language='en', test='', bert_scoring=False):
        super(LitSAFT5, self).__init__()
        self.wq = with_questions
        self.label = label
        self.bert_scoring = bert_scoring
        self.language = language
        # Load model and tokenizer
        if language == 'ger':
            print("Using mt5 Model")
            self.model = AutoModelForSeq2SeqLM.from_pretrained("google/mt5-base")
            self.tokenizer = AutoTokenizer.from_pretrained("google/mt5-base")
            self.folder = 'preprocessed/german'
        elif language == 'en':
            print("Using T5 Model")
            self.model = T5ForConditionalGeneration.from_pretrained('t5-base')
            self.tokenizer = T5Tokenizer.from_pretrained('t5-base')
            self.folder = 'preprocessed/english'
        else:
            raise ValueError("Unsupported language or wrong string")

        if with_questions:
            self.mode = 'wq_' + label
        else:
            self.mode = label

        print("Training/Testing on the following datasets: ", language, self.mode)

        # Load dataset
        data = dl.T5Dataset(self.folder + '/' + self.mode + '_train.npy')
        self.test_data = dl.T5Dataset(self.folder + '/' + self.mode + '_ua.npy')
        if test:
            self.test = test

        self.batch_size = batch_size
        # Load and split data
        self.train_data, self.val_data = random_split(data, split(len(data)),
                                                      generator=torch.Generator().manual_seed(42))
        self.save_hyperparameters()

    def forward(self, tok_seq, attn_seq):
        # force min length of prediction
        state = self.tokenizer.decode(self.model.generate(input_ids=tok_seq, attention_mask=attn_seq, min_length=11,
                                                          max_length=128)[0], skip_special_tokens=True)
        return state

    def training_step(self, batch, batch_idx):
        text, text_attn, answer, lab = batch
        loss = self.model(input_ids=text, attention_mask=text_attn, labels=answer)[0].mean()
        return loss

    def validation_step(self, batch, batch_idx):
        text, text_attn, answer, lab = batch
        return {'prediction': self(text, text_attn),
                'truth': self.tokenizer.decode(answer.squeeze(), skip_special_tokens=True),
                'label': self.tokenizer.decode(lab.squeeze(), skip_special_tokens=True),
                'original': self.tokenizer.decode(text.squeeze(), skip_special_tokens=True),
                }

    def validation_epoch_end(self, outputs):
        # validation array: first entry are all full text predictions, second entry gold standard, third entry label
        # and fourth entry label prediction
        if self.label == 'score':
            val_data = [[x['prediction'] for x in outputs], [x['truth'] for x in outputs],
                        [x['label'] for x in outputs], [x['prediction'].split(' ', 1)[0] for x in outputs]]

            pred = extract_model_pred(val_data[0])
            truth = [x.split(' ', 2)[2] for x in val_data[1]]
            acc_data = np.array(val_data[2:])
            # mse
            if len(acc_data[1]) > 0:
                mse_val, invalid = mse(acc_data[1], acc_data[0])
                self.log('mse', mse_val)
            else:
                print('\nInvalid mse')
                mse_val, invalid = 1
                self.log('mse', 0)

        elif self.label == 'ver':
            val_data = [[x['prediction'] for x in outputs], [x['truth'] for x in outputs],
                        [x['label'] for x in outputs]]
            pred = extract_pred(val_data[0])
            truth = [x.split(':', 1)[1] for x in val_data[1]]
            label_pred = extract_label(val_data[0])
            acc_data = np.array([val_data[2], label_pred])
            val_acc = metrics.f1_score(acc_data[0], label_pred)
            val_weighted = metrics.f1_score(acc_data[0], label_pred, average='weighted')
            val_macro = metrics.f1_score(acc_data[0], label_pred, average='macro',
                                         labels=['incorrect', 'partially correct', 'correct'])

            # calculate model selection metrics
        sacrebleu_score = sacrebleu.compute(predictions=pred,
                                            references=[[x] for x in truth])['score']
        rouge_score = rouge.compute(predictions=pred, references=truth)['rouge2'].mid.fmeasure
        meteor_score = meteor.compute(predictions=pred, references=truth)['meteor']

        # log custom metric
        if self.label == 'score':
            self.log('my_metric', (sacrebleu_score / 100 + rouge_score + meteor_score) / 3 * (1 - mse_val) *
                     (1 - invalid / len(acc_data[1])))
            print('MSE = {:.4f}, BLEU = {:.4f}, Rouge = {:.4f}, Meteor = {:.4f}'
                  .format(mse_val, sacrebleu_score, rouge_score, meteor_score))
        elif self.label == 'ver':
            self.log('my_metric', (sacrebleu_score / 100 + rouge_score + meteor_score) / 3 * val_macro)
            self.log('val_macro', val_macro)
            print('Acc = {:.4f}, M-F1 = {:.4f}, W-F1 = {:.4f}, BLEU = {:.4f}, Rouge = {:.4f}, Meteor = {:.4f}'
                  .format(val_acc, val_macro, val_weighted, sacrebleu_score, rouge_score, meteor_score))

        self.log('sacreBleu', sacrebleu_score)
        self.log('ROUGE', rouge_score)
        self.log('METEOR', meteor_score)

    def test_step(self, batch, batch_idx):
        text, text_attn, answer, lab = batch
        # print("Test stepping")
        return {'prediction': self(text, text_attn),
                'truth': self.tokenizer.decode(answer.squeeze(), skip_special_tokens=True),
                'label': self.tokenizer.decode(lab.squeeze(), skip_special_tokens=True),
                'original': self.tokenizer.decode(text.squeeze(), skip_special_tokens=True),
                }

    def test_epoch_end(self, outputs):
        # validation array: first entry are all full text predictions, second entry gold standard, third entry label
        # and fourth entry label prediction
        if self.label == 'score':
            test_data = [[x['prediction'] for x in outputs], [x['truth'] for x in outputs],
                        [x['label'] for x in outputs], [x['prediction'].split(' ', 1)[0] for x in outputs]]

            pred = extract_model_pred(test_data[0])
            truth = [x.split(' ', 2)[2] for x in test_data[1]]
            acc_data = np.array(test_data[2:])
            # mse
            if len(acc_data[1]) > 0:
                mse_val, invalid = mse(acc_data[1], acc_data[0])
                self.log('mse', mse_val)
            else:
                print('\nInvalid mse')
                mse_val, invalid = 1
                self.log('mse', 0)

        elif self.label == 'ver':
            test_data = [[x['prediction'] for x in outputs], [x['truth'] for x in outputs],
                        [x['label'] for x in outputs]]
            pred = extract_pred(test_data[0])
            truth = [x.split(':', 1)[1] for x in test_data[1]]
            label_pred = extract_label(test_data[0])
            acc_data = np.array([test_data[2], label_pred])
            val_acc = metrics.accuracy_score(acc_data[0], label_pred)
            val_weighted = metrics.f1_score(acc_data[0], label_pred, average='weighted')
            val_macro = metrics.f1_score(acc_data[0], label_pred, average='macro',
                                         labels=['incorrect', 'partially correct', 'correct'])

            # calculate model selection metrics
        sacrebleu_score = sacrebleu.compute(predictions=pred,
                                            references=[[x] for x in truth])['score']
        rouge_score = rouge.compute(predictions=pred, references=truth)['rouge2'].mid.fmeasure
        meteor_score = meteor.compute(predictions=pred, references=truth)['meteor']

        # log custom metric
        if self.label == 'score':
            self.log('my_metric', (sacrebleu_score / 100 + rouge_score + meteor_score) / 3 * (1 - mse_val) *
                     (1 - invalid / len(acc_data[1])))
            print('\nMSE = {:.4f}, BLEU = {:.4f}, Rouge = {:.4f}, Meteor = {:.4f}'
                  .format(mse_val, sacrebleu_score, rouge_score, meteor_score))
        elif self.label == 'ver':
            self.log('my_metric', (sacrebleu_score / 100 + rouge_score + meteor_score) / 3 * val_macro)
            self.log('val_macro', val_macro)
            print('\nAcc = {:.4f}, M-F1 = {:.4f}, W-F1 = {:.4f}, BLEU = {:.4f}, Rouge = {:.4f}, Meteor = {:.4f}'
                  .format(val_acc, val_macro, val_weighted, sacrebleu_score, rouge_score, meteor_score))

        self.log('sacreBleu', sacrebleu_score)
        self.log('ROUGE', rouge_score)
        self.log('METEOR', meteor_score)

        if self.bert_scoring:
            save('models/' + self.mode + '/' + '_'.join([self.language, self.test]), test_data)

    def configure_optimizers(self):
        return Adafactor(self.model.parameters(), lr=None, warmup_init=True, relative_step=True)

    def train_dataloader(self):
        return DataLoader(self.train_data, batch_size=self.batch_size, num_workers=0, shuffle=False)

    def val_dataloader(self):
        return DataLoader(self.val_data, batch_size=1, num_workers=0, shuffle=False)

    def test_dataloader(self):
        return DataLoader(self.test_data, batch_size=1, num_workers=0, shuffle=False)