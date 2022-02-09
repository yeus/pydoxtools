#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Mon Mar 30 15:29:52 2020

@author: Thomas.Meschede@soprasteria.com

TODO: refactor this... there are a lot of functions and it would
      be great if we could somehow organize them a little better...

TODO: think about how to disribute functions between nlp_utils and
      classifier
"""

import logging

logger = logging.getLogger(__name__)

import torch
import functools
import transformers as transformers
# from transformers import pipeline #for sentiment analysis

import spacy
import sklearn as sk
import sklearn.linear_model

compare = sklearn.metrics.pairwise.cosine_similarity

import pandas as pd
import numpy as np
from tqdm import tqdm
from pydoxtools import html_utils
from pydoxtools.settings import settings
from urlextract import URLExtract
from difflib import SequenceMatcher
from scipy.spatial.distance import pdist, squareform


def str_similarity(a, b):
    """
    return string similarity [0..1]
  
    TODO: consider doing this with the levenshtein which is a bit faster
    """
    return SequenceMatcher(None, a, b).ratio()


device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
logger.info(f"using {device}-device for nlp_operations!")

memory = settings.get_memory_cache()
urlextractor = URLExtract(extract_email=True, cache_dns=True, extract_localhost=True)
urlextractor.update_when_older(7)  # updates when list is older that 7 days

# TODO: enhance this with "textrank" algorithms
def self_similarity_matrix(strlist):
    """
    calculates self-similarity of strings using
    pythons difflib in a matrix

    TODO: get rid of pandas dataframe here
    """

    def sim(u, v):
        return str_similarity(u[0].lower(), v[0].lower())

    s = squareform(pdist(strlist.values.reshape(-1, 1), sim))
    return pd.DataFrame(s)  # , columns=strlist, index=strlist)


def get_urls_from_text(text):
    urls = urlextractor.find_urls(text, only_unique=False, check_dns=True)
    return urls


@functools.lru_cache()
def get_spacy_model(model_type, size="sm"):
    logger.info(f"load spacy model: {model_type}")
    if model_type == 'en':
        return spacy.load(f'en_core_web_{size}')
    elif model_type == 'de':
        return spacy.load(f'de_core_news_{size}')
    else:
        return spacy.load('xx_ent_wiki_sm')


def tokenize_windows(txt, tokenizer, win_len=500, overlap=50,
                     max_len=510, add_special_tokens=True):
    # token_ids = tokenizer.encode(txt,add_special_tokens=False)
    toktxt = tokenizer.tokenize(txt)
    # tokenizer.convert_tokens_to_ids(toktxt)

    tk_num = len(toktxt)
    if tk_num < max_len:
        toktxt = tokenizer.encode(txt)
        if add_special_tokens:
            return [toktxt], toktxt
        else:
            return [toktxt[1:-1]], toktxt
    else:
        step = int(win_len - overlap)

        steps = list(range(0, tk_num, step))
        tok_wins = [toktxt[idx:idx + win_len] for idx in steps]

        # add [CLS] and [SEP] tokens to the windows and encode into ids
        cls_tok = tokenizer.special_tokens_map['cls_token']
        sep_tok = tokenizer.special_tokens_map['sep_token']
        if add_special_tokens:
            tok_wins = [[cls_tok] + win + [sep_tok] for win in tok_wins]
        tok_wins_ids = [tokenizer.convert_tokens_to_ids(win) for win in tok_wins]
        return tok_wins_ids, toktxt


def transform_to_contextual_embeddings(input_ids_t, model, tokenizer=None, lang=False):
    # for one sentence all ids are "1" for two, the first sentence gets "0"
    input_ids_t = torch.tensor([input_ids_t]).to(device)
    segments_ids_t = torch.tensor([[1] * input_ids_t.shape[1]]).to(device)

    # create language IDs
    if lang:
        language_id = tokenizer.lang2id[lang]  # 0
        langs_t = torch.tensor([language_id] * input_ids_t.shape[1])  # torch.tensor([0, 0, 0, ..., 0])
        # We reshape it to be of size (batch_size, sequence_length)
        langs_t = langs_t.view(1, -1)  # is

    # Predict hidden states features for each layer
    with torch.no_grad():
        if lang:  # if XLM cross lingual models
            encoded_layers = model(input_ids_t, langs=langs_t)
            wvecs_out = encoded_layers[0].detach().cpu().numpy()
        else:  # if using BERT-likes/multilingual
            encoded_layers = model(input_ids_t, segments_ids_t)
            # distilbert:
            # remove [CLS] and [SEP] tokens
            wvecs_out = encoded_layers[0][0].cpu().numpy()[1:-1]
            # wvecs_out = encoded_layers[1][-1][0].numpy()[1:-1]
            # this one is for "normal" BERT:
            # wvecs_out = encoded_layers[2][-1][0].numpy()[1:-1]
            # wvecs_out = encoded_layers[2][-1][0].numpy()[1:-1]
    return wvecs_out


# TODO: prepare for long texts to do the tokenization in batches
# otherwise well run out of memory :(
@memory.cache(ignore=["tokenizer"])
def longtxt_word_embeddings_fullword_vocab(txt, tokenizer):
    """
    generate whole-word embeddings (without pseudo-syllables)
    using only transformers tokenizer without
    model.
    """
    vs, toktxt = get_embeddings(txt, tokenizer)
    return fullword_embeddings(toktxt, vs)


@memory.cache(ignore=["model", "tokenizer"])
def longtxt_embeddings_fullword(txt, model, tokenizer):
    """
    generate whole-word embeddings (without pseudo-syllables)
    using transformers models.
    """
    vs, toktxt = longtxt_embeddings(txt, model, tokenizer)
    return fullword_embeddings(toktxt, vs)


@memory.cache(ignore=["model", "tokenizer"])
def longtxt_embeddings(txt, model, tokenizer,
                       pooling=None,
                       overlap=50,
                       longtextcap=True):
    """
    generate wordpiece embeddings (pseudo-syllables) using transformer model
    and text windowing. The individual windows are stitched
    back together at the and by averaging their values

    TODO: add option to cancel embeddings generation afte a certain
          number of windows to make sure it finishes in a guaranteed time
    """
    tok_wins, toktxt = tokenize_windows(txt, tokenizer=tokenizer,
                                        overlap=overlap)
    if longtextcap:
        tok_wins = tok_wins[:100]
    if len(tok_wins) > 100:  # only use tqdm for "long lasting" transformations
        vec_wins = [transform_to_contextual_embeddings(win, model=model)
                    for win in tqdm(tok_wins)]
    else:
        vec_wins = [transform_to_contextual_embeddings(win, model=model)
                    for win in tok_wins]
    # pd.DataFrame(vec_wins).shapeFalse

    if len(vec_wins) == 1:
        vecs = vec_wins[0]
    else:
        win_len = vec_wins[0].shape[0]
        step = int(win_len - overlap)
        vecs = vec_wins[0][:step]
        for i in range(len(vec_wins) - 1):
            # average overlapping vectors of current and next window
            nxtvecs = (vec_wins[i][step:] + vec_wins[i + 1][:overlap]) / 2
            vecs = np.vstack((vecs, nxtvecs))
            vecs = np.vstack((vecs, vec_wins[i + 1][overlap:step]))
        # vecs = np.vstack((vecs,vec_wins[-1][step:]))

    if pooling == None:
        return vecs, toktxt
    else:
        return pooling(vecs, axis=0), toktxt


# old name: create_cross_lingual_embeddings
def create_cross_lingual_contextual_embeddings(txt, model, tokenizer, lang=False):
    # Map the token strings to their vocabulary indeces.
    # indexed_tokens = tokenizer.convert_tokens_to_ids(toktxt)
    input_ids_t = torch.tensor([tokenizer.encode(txt)])

    # for one sentence all ids are "1" for two, the first sentence gets "0"
    segments_ids_t = torch.tensor([[1] * input_ids_t.shape[1]])

    # create language IDs
    if lang:
        language_id = tokenizer.lang2id[lang]  # 0
        langs_t = torch.tensor([language_id] * input_ids_t.shape[1])  # torch.tensor([0, 0, 0, ..., 0])
        # We reshape it to be of size (batch_size, sequence_length)
        langs_t = langs_t.view(1, -1)  # is

    # Predict hidden states features for each layer
    with torch.no_grad():
        if lang:  # if XLM cross lingual models
            encoded_layers = model(input_ids_t, langs=langs_t)
            wvecs_out = encoded_layers[0].detach().numpy()
        else:  # if using BERT-likes
            encoded_layers = model(input_ids_t, segments_ids_t)
            wvecs_out = encoded_layers[0].numpy()

    return wvecs_out


def cos_compare(X, Y):
    return sk.metrics.pairwise.cosine_similarity(X, Y)


"""
def build_pipe(X, params):
    classifiers=[
        sk.linear_model.LogisticRegression(max_iter=200,
                                            verbose=1,
                                            n_jobs=-1),
        sk.linear_model.LogisticRegressionCV(),
        sk.linear_model.RidgeClassifier(),
        sk.svm.LinearSVC(),
        sk.linear_model.SGDClassifier(),
        sk.linear_model.PassiveAggressiveClassifier(),
        sk.naive_bayes.BernoulliNB(alpha=.01), 
        sk.naive_bayes.ComplementNB(alpha=.01), 
        sk.naive_bayes.MultinomialNB(alpha=.01),
        sk.neighbors.KNeighborsClassifier(),
        sk.neighbors.NearestCentroid(),
        sk.ensemble.RandomForestClassifier(),
        ]
    
    classifier = classifiers[params['classifier']]
    logger.info(f"using classifier: {classifier}")

    txt_tf = sk.pipeline.make_pipeline(
            sk.preprocessing.FunctionTransformer(
                    select_long_description,
                    validate=False), #has to be "False" to allow strings
            TfidfVectorizer(
                tokenizer = nc_tokenizer,
                max_df = params["maxdf"],
                max_features=params['maxfeat'],
                ngram_range=params["ngram_range"],
                preprocessor=None)
            )

    #txt_tf.fit(X_train,y_train)
    #tfidf_vector
    logger.info("selecting columns")
    select_cols = select_feature_columns(X,max_unique_cat=params['max_unique_cat'])
    
    cat_tf = skl.pipeline.make_pipeline(
            skl.preprocessing.FunctionTransformer(
                    select_columns,validate=False,
                    kw_args={'columns':select_cols}),
            skl.impute.SimpleImputer(strategy='constant', fill_value='missing'),
            skl.preprocessing.OneHotEncoder(handle_unknown='ignore'))
    
    pipe = skl.pipeline.make_pipeline(
            skl.pipeline.make_union(txt_tf, cat_tf),
            #skl.decomposition.TruncatedSVD(n_components=1000),
            classifier)

    return pipe
"""


@functools.lru_cache()
@memory.cache
def get_bert_vocabulary():
    model, tokenizer = load_models()
    # return and transform embeddings into numpy array
    return model.embeddings.word_embeddings.weight.detach().numpy()


@functools.lru_cache(maxsize=32)
def load_tokenizer():
    logger.info("load_tokenizer")
    tokenizer = transformers.DistilBertTokenizer.from_pretrained('distilbert-base-multilingual-cased')
    return tokenizer, get_vocabulary()


@functools.lru_cache()
def load_models():
    logger.info(f"load model on device: {device}")
    # tokenizer = ts.BertTokenizer.from_pretrained('bert-base-multilingual-cased')
    # config = ts.BertConfig.from_pretrained('bert-base-multilingual-cased', output_hidden_states=True)
    # model = ts.BertModel.from_pretrained('bert-base-multilingual-cased', config=config)
    tokenizer = transformers.DistilBertTokenizer.from_pretrained('distilbert-base-multilingual-cased')
    config = transformers.DistilBertConfig.from_pretrained('distilbert-base-multilingual-cased',
                                                           output_hidden_states=True)
    model = transformers.DistilBertModel.from_pretrained('distilbert-base-multilingual-cased', config=config)

    model.to(device)
    model.eval()
    return model, tokenizer


def veclengths(x):
    return np.sqrt((x * x).sum(axis=1))


def maxlens(x):
    return np.max(x, axis=1)


def vecseq_similarity(vs, search_vec):
    return cos_compare(vs, [search_vec])


@functools.lru_cache()
def get_vocabulary(kind="distilbert"):
    """make sure the vocabulary only gets loaded once
    TODO: implement more vocabularies"""
    logger.info("loading BERT vocabulary")
    return get_bert_vocabulary()


def get_embeddings(txt, tokenizer):
    """
    generate word-piece embeddings (pseudo-syllables)
    using only transformers tokenizer without
    model.
    """
    txttok = tokenizer.tokenize(txt)
    tok_ids = tokenizer.convert_tokens_to_ids(txttok)
    tok_vecs = get_vocabulary()[tok_ids]
    return tok_vecs, txttok


@memory.cache
def fullword_embeddings(toktxt, vs):
    """
    get embeddings for entire words by sowing wordpieces back together.

    Parameters
    ----------
    toktxt : tokenized text
        DESCRIPTION.
    vs : word piece vectors
        DESCRIPTION.

    Returns
    -------
    numpy.ndarray, numpy.ndarray
        return full word-tokens and vectors

    """
    # average embedding vectors for entire words:
    emb_map = list(zip(toktxt, vs))

    syl_sep = ""  # could also be "-" for example

    newtoks = []
    newvs = []
    cur_word = ""
    cur_vec = []
    for tok, v in emb_map:
        if len(tok) >= 3:
            if tok[:2] == '##':
                cur_word += tok
                cur_vec += [v]
                continue
        newtoks += [cur_word.replace("##", syl_sep)]
        newvs += [np.mean(cur_vec, axis=0)]
        cur_vec = [v]
        cur_word = tok

    newtoks += [cur_word.replace("##", syl_sep)]
    newvs += [np.mean(cur_vec, axis=0)]

    # newtoks = [txt.encode("windows-1252").decode("utf-8")
    #               for txt in newtoks[1:]]
    return np.array(newvs[1:]), np.array(newtoks[1:])


def top_search_results(toktxt, match, num=10):
    """
    returns:
        best matching tokens, their ids and corresponding
        scores
    """
    toktxt = np.array(toktxt)
    idxs = np.argpartition(match.flatten(), -num)[-num:][::-1]
    idxs = idxs[np.argsort(match[idxs][:, 0])][::-1]
    return toktxt[idxs], idxs, match[idxs]


def get_max_word_similarity(vs, searchstring, model, tokenizer):
    sv, _ = get_embeddings(searchstring, model, tokenizer)
    match = vecseq_similarity(vs, sv.mean(axis=0))
    return match.max()


def search(toktxt, vs, searchstring, model, tokenizer, num=1):
    """
    returns:
        top tokens, token ids, correponding scores, all token scores
    """
    # sv, _ = longtxt_embeddings(search_word,model,tokenizer)
    sv, _ = get_embeddings(searchstring, tokenizer)

    match = vecseq_similarity(vs, sv.mean(axis=0))
    return top_search_results(toktxt, match, num=num) + (match,)


def get_keywords():
    raise NotImplementedError
    # justsome ideas in the following
    # TODO: generate "vs" using transform_to_contextual_embeddings
    # TODO: maybe to a ANN search with the vocabulary from BERT?
    similarity = nlpu.vecseq_similarity(vs, sentvec)
    wordranks = pd.DataFrame(zip(similarity, tokwords),
                             columns=['similarity', 'words'])
    wordranks['importance'] = importance * wordranks['similarity']
    #colhtml = html_utils.color_text(tokwords, similarity)
    #oib = html_utils.oib
    #oib(colhtml)


# def topic_similarity(model):

# these urls were selected, because they have particularly long webpages
# to slow down the classifiers etc...
example_urls = [
    "https://www.newark.com/c/passive-components/resistors-fixed-value",
    "https://www.newark.com/c/test-measurement/test-equipment-accessories",
    "https://www.newark.com/c/enclosures-racks-cabinets/enclosures-boxes-cases",
    "https://www.newark.com/c/circuit-protection/tvs-transient-voltage-suppressors",
    "https://chicagodist.com/collections/pololu",
    "https://www.newark.com/c/semiconductors-discretes/transistors",
    "https://chicagodist.com/collections/all-in-stock-items",
    "https://buyzero.de/products/raspberry-pi-4-model-b?variant=28034033287270",
    "https://buyzero.de/products/raspberry-pi-4-model-b?variant=28034033090662",
    "https://buyzero.de/products/raspberry-pi-4-model-b?variant=28034034008166",
]


@memory.cache
def string_embeddings(text, method="fast"):
    """
    this method converts a text of arbitrary length into
    a vector.
    """
    if method == "fast":
        tokenizer, _ = load_tokenizer()
        vs, toktxt = get_embeddings(text_short, tokenizer)
        vs = vs.mean(axis=0)
    elif method == "slow":
        model, tokenizer = load_models()
        vs, toktxt = longtxt_embeddings(
            text_short, model, tokenizer, np.mean)
    return vs


def page2vec(page_str, url=None, method="slow"):
    """
    TODO: use the document class for this....

    calculate a fingerprint from any arbitrary webpage
    
    TODO: include potential incoming links in fingerprint.
        Those would have to be search independently
    
    TODO: include more information into vectorization such as
    - tag-density
    - link-density
    - nomalized pagelength
    
    - which tags
    - structure of html
    - screenshot of page
    """
    # +length = len(html)
    # length = len(" ".join(html.split()))
    if method == "no_embeddings":
        vs = [
            len(url),
            len(page_str),
        ]
    elif method in ["slow", "fast"]:
        try:
            # TODO: can we somehow move html_utils out of this file?
            text_short = html_utils.get_pure_html_text(text)
            vs = string_embeddings(text_short, method)
        except:
            logger.exception(f"can not convert: {url}")
            return None

    vectorization = vs
    return vectorization


# TODO implement this function for reinforcement link following
@memory.cache
def link2vec(source_html, source_url, link_context, link_url):
    raise NotImplementedError()


def topic_similarity(html, topic, method="slow"):
    """
    compare similarity of a given html page
    with a certain topic.
    
    The result has a range between 0..1 but
    usually results will be in the range
    [0..
     TODO: (~0.01)]
    
    TODO: make a more "finegrained analysis" 
    """
    vs = page2vec(html, method)

    model, tokenizer = load_models()
    sv, _ = longtxt_embeddings(topic, model, tokenizer, np.mean)
    # sv, _ = get_embeddings(searchstring,tokenizer)

    similarity = cos_compare([vs], [sv])
    # match = vecseq_similarity(vs,sv.mean(axis=0))

    return similarity


def extract_entities_spacy(text, nlp):
    doc = nlp(text)
    return [(ent.text, ent.label_) for ent in doc.ents]


# TODO: we need to move this into the nlp_context class
#      and provide the use of the models with a timeout
#      or a "with" context or something similar...

# @functools.lru_cache()


def convert_ids_to_string(tokenizer, ids):
    a = tokenizer.convert_ids_to_tokens(ids)
    return tokenizer.convert_tokens_to_string(a)
