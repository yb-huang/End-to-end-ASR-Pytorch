import sys
sys.path.insert(0, '..')
from src.preprocess import extract_feature,encode_target
from joblib import Parallel, delayed
import argparse
import os 
import csv
from pathlib import Path
from os.path import join
from tqdm import tqdm
import pickle
import numpy as np
import pandas as pd
from subprocess import call
from operator import itemgetter

"""
1. File/directory names with <> can be modified, otherwise fixed.
2. lm_corpus.txt is generated for language model training.
3. Besides transcripts, other text sequences can also be included in lm_corpus.txt for language model training.

Directory tree should be like...
<data_path>(corpus name)
├── <train>
|   ├── audio
|   |   ├── <1.mp3>
|   |   └── <2.mp3>
|   └── transcript.csv
├── <test>
|   ├── audio
|   |   ├── <1.mp3>
|   |   └── <2.mp3>
|   └── transcript.csv
└── lm_corpus.txt

In transcript.csv...
audio,text
<1.mp3>,<How are you?>
<2.mp3>,<How's it going?>

In lm.corpus.txt...
<How are you?>   
<How's it going?>
...
<This is an example.>
"""

def change_fe(cur_path, chg_dir, file_extension):
    """ change file extension """
    _, file_name = os.path.split(cur_path)
    base = os.path.splitext(file_name)[0]
    return join(chg_dir, base+file_extension)

def read_text(paras, sets):
    # Select dataset to train bpe
    print('Generate Vocabulary for {} unit.'.format(paras.target))
    for idx, s in enumerate(sets):
        print('\t', idx, ':', s)
    bpe_tr = input('Please enter the indices of training sets for generating vocabulary (seperate w/ space): ')
    bpe_tr = [sets[int(t)] for t in bpe_tr.split(' ')]

    # Collect text
    tr_txt = []
    for s in bpe_tr:
        with open(join(paras.data_path, s, 'transcript.csv'), 'r') as f:
            rows = csv.reader(f)
            texts = list(zip(*rows))[1][1:]
            tr_txt += texts
    return tr_txt

def generate_table(paras, tr_txt):
    ### Step 1. Calculate wrd frequency
    encode_table = {'<sos>':1e10, '<eos>':1e10, '<unk>':1e10}
    for target in tr_txt:
        for t in target.split(' '):
            if t not in encode_table:
                encode_table[t] = 1
            else:
                encode_table[t] += 1
    ### Step 2. Top k list for encode map
    max_idx = min(paras.n_tokens, len(encode_table))
    print('Origin vocabulary size: ', len(encode_table))
    all_tokens = [k for k,v in sorted(encode_table.items(), key=itemgetter(1), reverse=True)][:max_idx]
    encode_table = {'<sos>':0, '<eos>':1, '<unk>':2}
    for tok in all_tokens:
        if tok not in encode_table:
            encode_table[tok] = len(encode_table)
    return encode_table

def bpe(paras, corpus, dim, sets, bpe_dir, tr_txt):
    # Train BPE
    with open(join(bpe_dir, 'transcript.txt'), 'w') as tf:
        for text in tr_txt:
            tf.write(text + '\n')

    call(['spm_train',
          '--input=' + os.path.join(bpe_dir, 'transcript.txt'),
          '--model_prefix=' + os.path.join(bpe_dir, 'bpe'),
          '--vocab_size=' + str(paras.n_tokens),
          '--character_coverage=1.0'
        ])
    # Encode data
    for s in sets:
        with open(join(paras.data_path, s, 'transcript.csv'), 'r') as f:
            rows = csv.reader(f)
            texts = list(zip(*rows))[1][1:]
            with open(join(bpe_dir,'raw',s+'.txt'), 'w') as f:
                for sent in texts: f.write(sent+'\n')
            call(['spm_encode',
                  '--model='+os.path.join(bpe_dir,'bpe.model'),
                  '--output_format=piece'
                ],stdin=open(join(bpe_dir,'raw',s+'.txt'),'r'),
                  stdout=open(join(bpe_dir,'encode',s+'.txt'),'w'))

    # Make Dict
    encode_table = {'<sos>':0,'<eos>':1}
    with open(join(bpe_dir,'bpe.vocab'), 'r', encoding="utf-8") as f:
        for line in f:
            tok = line.split('\t')[0]
            if tok not in ['<s>', '</s>']:
                encode_table[tok] = len(encode_table)
    return encode_table

def feature_extract(paras, sets, output_dir, encode_table, bpe_dir):
    print('Data sets :')
    for idx, s in enumerate(sets):
        print('\t', idx, ':', s)
    tr_set = input('Please enter the index of splits you wish to use preprocess. (seperate with space): ')
    tr_set = [sets[int(t)] for t in tr_set.split(' ')]
    for s in tr_set:
        audio_dir = join(paras.data_path, s, 'audio')
        todos = []
        with open(join(paras.data_path, s, 'transcript.csv'), 'r') as f:
            rows = csv.reader(f)
            todos = list(zip(*rows))[0][1:]
            todos = [join(audio_dir, f) for f in todos]
        print('Encoding target...', flush=True)
        tr_y = []
        if paras.target == 'subword':
            with open(join(bpe_dir, 'encode', s+'.txt'), 'r') as f:
                for line in f: tr_y.append(line[:-1].split(' '))
        else:
            with open(join(paras.data_path, s, 'transcript.csv'), 'r') as f:
                rows = csv.reader(f)
                tr_y = list(zip(*rows))[1][1:]
                tr_y = [seq.split(' ') for seq in tr_y]
        tr_y = encode_target(tr_y, encode_table)
        cur_path = os.path.join(output_dir, s)
        if not os.path.exists(cur_path): os.makedirs(cur_path)
        print('Extracting acoustic feature...{}'.format(s))
        tr_x = Parallel(n_jobs=paras.n_jobs)(delayed(extract_feature)(str(file_path),feature=paras.feature_type,dim=paras.feature_dim,\
                    cmvn=paras.apply_cmvn,delta=paras.apply_delta,delta_delta=paras.apply_delta_delta,\
                    save_feature=change_fe(file_path, cur_path, '')) for file_path in tqdm(todos)) # sort by len
        sorted_idx = list(reversed(np.argsort(tr_x)))
        sorted_y = ['_'.join([str(i) for i in tr_y[idx]]) for idx in sorted_idx]
        sorted_todos = [change_fe(str(todos[idx]), s, '.npy') for idx in sorted_idx]
        # Dump label
        df = pd.DataFrame(data={'file_path':[fp for fp in sorted_todos],'length':list(reversed(sorted(tr_x))),'label':sorted_y})
        df.to_csv(join(output_dir, s+'.csv'))

        with open(join(output_dir,"mapping.pkl"), "wb") as fp:
            pickle.dump(encode_table, fp)

    return encode_table

def lm_corpus(paras, encode_table, bpe_dir):
    print('Generate langauge model corpus')
    tr_y = []
    if paras.target == 'subword':
        call(['spm_encode',
              '--model='+os.path.join(bpe_dir, 'bpe.model'),
              '--output_format=piece'
            ],stdin=open(join(paras.data_path, 'lm_corpus.txt'), 'r'),
              stdout=open(join(bpe_dir, 'encode', 'lm_corpus.txt'), 'w'))
        with open(join(bpe_dir, 'encode', 'lm_corpus.txt'), 'r') as f:
            for line in f: tr_y.append(line[:-1].split(' '))
    else:
        with open(join(paras.data_path, 'lm_corpus.txt'), 'r') as f:
            for line in f: tr_y.append(line.split(' '))
    tr_y = encode_target(tr_y, table=encode_table)
    tr_y = ['_'.join([str(x) for x in y]) for y in tr_y]
    num_valid = min(20000, len(tr_y)//10)
    va_y = tr_y[-num_valid:]
    tr_y = tr_y[:-num_valid]
    dummy = ['' for _ in tr_y]
    df = pd.DataFrame(data={'file_path':dummy, 'length': dummy, 'label':tr_y})
    df.to_csv(join(output_dir, 'lm_corpus.csv'))
    dummy = ['' for _ in va_y]
    df = pd.DataFrame(data={'file_path':dummy, 'length': dummy, 'label':va_y})
    df.to_csv(join(output_dir, 'lm_corpus_valid.csv'))
    print('All done, saved at', output_dir, 'exit.')

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Preprocess program for Corpus.')
    parser.add_argument('--data_path', type=str, help='List of path to raw dataset')
    parser.add_argument('--feature_type', default='fbank', type=str, help='Feature type ( mfcc / fbank )', required=False)
    parser.add_argument('--feature_dim', default=40, type=int, help='Dimension of feature', required=False)
    parser.add_argument('--apply_delta', default=True, type=bool, help='Append Delta', required=False)
    parser.add_argument('--apply_delta_delta', default=False, type=bool, help='Append Delta Delta', required=False)
    parser.add_argument('--apply_cmvn', default=True, type=bool, help='Apply CMVN on feature', required=False)
    parser.add_argument('--output_path', default='.', type=str, help='Path to store output', required=False)
    parser.add_argument('--n_jobs', default=-1, type=int, help='Number of jobs used for feature extraction', required=False)
    parser.add_argument('--n_tokens', default=5000, type=int, help='Vocabulary size of target', required=False)
    parser.add_argument('--target', default='subword', type=str, help='Learning target ( subword / word )', required=False)
    paras = parser.parse_args()

    dim = paras.feature_dim * (1+paras.apply_delta+paras.apply_delta_delta)
    corpus = os.path.basename(os.path.normpath(paras.data_path))
    sets = [d for d in os.listdir(paras.data_path) if os.path.isdir(join(paras.data_path, d))]

    # Setup path
    output_dir = join(paras.output_path, '_'.join([corpus, str(paras.feature_type)+str(dim), paras.target+str(paras.n_tokens)]))
    tr_txt = read_text(paras, sets)
    bpe_dir = None
    if paras.target == 'subword':
        bpe_dir = join(output_dir, 'bpe')
        if not os.path.exists(bpe_dir):
            os.makedirs(join(bpe_dir, 'raw'))
            os.makedirs(join(bpe_dir, 'encode'))
        # BPE training
        encode_table = bpe(paras, corpus, dim, sets, bpe_dir, tr_txt)
    else:
        encode_table = generate_table(paras, tr_txt)
    feature_extract(paras, sets, output_dir, encode_table, bpe_dir)
    lm_corpus(paras, encode_table, bpe_dir)
