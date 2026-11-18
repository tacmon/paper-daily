#!/usr/bin/env python3
"""Local, incremental E5 retrieval. Never calls a hosted model or downloads weights."""
from __future__ import annotations
import argparse
from collections import Counter
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
import unicodedata
import radar
from setup_model import MODEL_DIR, sha256

HERE = radar.HERE
DATA = HERE / 'data'
SPEC = json.loads((HERE / 'model.lock.json').read_text())
FINGERPRINT = hashlib.sha256(json.dumps(SPEC, sort_keys=True).encode()).hexdigest()
ALIASES = {
    '课程': 'curriculum', '教师': 'teacher', '学生': 'student',
    '非平稳': 'nonstationary non-stationary', '目标条件': 'goal-conditioned',
    '学习进展': 'learning progress', '强化学习': 'reinforcement learning',
    '奖励噪声': 'noisy reward', '反馈偏差': 'biased feedback', '能力': 'competence',
}


def document(paper):
    return '\n'.join(str(paper.get(k) or '') for k in ('title', 'title_zh', 'abstract')).strip()[:20000]


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def tokens(value, expand=False):
    value = unicodedata.normalize('NFKC', value).lower()
    if expand:
        value += ' ' + ' '.join(v for k, v in ALIASES.items() if k in value)
    result = re.findall(r'[a-z0-9]+', value)
    for phrase in re.findall(r'[\u4e00-\u9fff]+', value):
        result.extend(phrase[i:i+2] for i in range(max(1, len(phrase)-1)))
    return result


def lexical_scores(query, papers):
    """BM25 with explicit, inspectable Chinese topic aliases (the lexical baseline)."""
    query_tokens = set(tokens(query, True))
    docs = [tokens(document(p)+' '+p.get('venue','')+' '+' '.join(p.get('authors', []))) for p in papers]
    frequencies = [Counter(d) for d in docs]
    df = Counter(t for d in frequencies for t in d)
    average = sum(map(len, docs)) / max(1, len(docs)) or 1
    scores = {}
    for p, doc, freq in zip(papers, docs, frequencies):
        score = 0.0
        for token in query_tokens & freq.keys():
            idf = math.log(1+(len(docs)-df[token]+.5)/(df[token]+.5))
            tf = freq[token]
            score += idf*tf*2.2/(tf+1.2*(.25+.75*len(doc)/average))
        if score > 0:
            scores[p['id']] = score
    return scores


@contextmanager
def inference_slot():
    """Serialize all local embedding jobs, including CLI and workbench requests."""
    path = HERE.parent / '.runtime/semantic-inference.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('本地向量编码正在使用中，本次退回关键词检索；请稍后再试')
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class Encoder:
    def __init__(self, model_dir=MODEL_DIR):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.np = np
        manifest = model_dir / 'manifest.json'
        if not manifest.exists() or json.loads(manifest.read_text()) != SPEC:
            raise RuntimeError('本地模型尚未安装或版本不匹配；请先运行 setup_model.py')
        for item in SPEC['files']:
            path = model_dir / item['path']
            if not path.is_file() or path.stat().st_size != item['size'] or sha256(path) != item['sha256']:
                raise RuntimeError('本地模型完整性检查失败：' + item['path'])
        self.tokenizer = Tokenizer.from_file(str(model_dir / 'tokenizer.json'))
        self.tokenizer.enable_truncation(max_length=SPEC['max_length'])
        self.tokenizer.enable_padding(pad_id=1, pad_token='<pad>')
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(model_dir / 'onnx/model.onnx'), sess_options=options, providers=['CPUExecutionProvider'])
        self.inputs = {x.name for x in self.session.get_inputs()}

    def encode(self, texts, query=False):
        np = self.np
        prefix = SPEC['query_prefix'] if query else SPEC['document_prefix']
        batches = self.tokenizer.encode_batch([prefix+x for x in texts])
        ids = np.asarray([b.ids for b in batches], dtype=np.int64)
        mask = np.asarray([b.attention_mask for b in batches], dtype=np.int64)
        inputs = {'input_ids': ids, 'attention_mask': mask, 'token_type_ids': np.zeros_like(ids)}
        hidden = self.session.run(None, {k:v for k,v in inputs.items() if k in self.inputs})[0]
        if hidden.ndim != 3 or hidden.shape[-1] != SPEC['dimension']:
            raise RuntimeError('模型输出与已验证的池化约定不符')
        pooled = (hidden*mask[:,:,None]).sum(1)/mask.sum(1)[:,None]
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        if not np.isfinite(pooled).all() or (norms == 0).any():
            raise RuntimeError('模型输出无效')
        vectors = (pooled/norms).astype('<f4')
        return [(v.tobytes(), bool(b.overflowing)) for v,b in zip(vectors,batches)]


class Index:
    def __init__(self, path=DATA/'vectors.sqlite', encoder_factory=Encoder):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS embeddings(paper_id TEXT PRIMARY KEY, text_hash TEXT NOT NULL,
            fingerprint TEXT NOT NULL, dimension INTEGER NOT NULL, vector BLOB NOT NULL,
            truncated INTEGER NOT NULL, encoded_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS query_cache(hash TEXT PRIMARY KEY, vector BLOB NOT NULL, created_at TEXT);
        ''')
        self.factory = encoder_factory
        self._encoder = None

    def close(self):
        self.db.close()

    def encoder(self):
        if self._encoder is None:
            self._encoder = self.factory()
        return self._encoder

    def valid(self, papers):
        hashes = {p['id']: digest(document(p)) for p in papers}
        return {r['paper_id']: r for r in self.db.execute('SELECT * FROM embeddings WHERE fingerprint=? AND dimension=?', (FINGERPRINT,SPEC['dimension']))
                if hashes.get(r['paper_id']) == r['text_hash'] and len(r['vector']) == 4*SPEC['dimension']}

    def update(self, papers, limit=3000, seconds=180):
        start = time.monotonic()
        valid = self.valid(papers)
        pending = [p for p in papers if p['id'] not in valid]
        # New reading notes and useful abstracts must not wait behind tens of
        # thousands of title-only proceedings entries after a catalogue expansion.
        pending.sort(key=lambda p:(p.get('kind')!='note', not bool(p.get('abstract')), -p.get('score',0)))
        encoded = truncated = 0
        if pending:
            with inference_slot():
                encoder = self.encoder()
                for offset in range(0, min(limit,len(pending)), 4):
                    if time.monotonic()-start >= seconds:
                        break
                    batch = pending[offset:min(offset+4,limit)]
                    vectors = encoder.encode([document(p) for p in batch])
                    with self.db:
                        for p,(vector,cut) in zip(batch,vectors):
                            self.db.execute('INSERT OR REPLACE INTO embeddings VALUES(?,?,?,?,?,?,?)',
                                (p['id'],digest(document(p)),FINGERPRINT,SPEC['dimension'],vector,int(cut),radar.now()))
                            encoded += 1
                            truncated += int(cut)
        return {'model': SPEC['repo'], 'fingerprint': FINGERPRINT, 'total':len(papers), 'encoded':encoded,
                'reused':len(valid), 'pending':len(pending)-encoded, 'truncated_new':truncated, 'seconds':round(time.monotonic()-start,3)}

    def dense_scores(self, query, papers):
        import numpy as np
        valid = self.valid(papers)
        if not valid:
            raise RuntimeError('没有与当前文档和模型版本匹配的向量；请更新索引')
        key = digest(FINGERPRINT+'\n'+query)
        cached = self.db.execute('SELECT vector FROM query_cache WHERE hash=?',(key,)).fetchone()
        if cached and len(cached[0]) == 4*SPEC['dimension']:
            vector = cached[0]
        else:
            with inference_slot():
                vector = self.encoder().encode([query],query=True)[0][0]
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO query_cache VALUES(?,?,?)',(key,vector,radar.now()))
                self.db.execute('DELETE FROM query_cache WHERE hash NOT IN (SELECT hash FROM query_cache ORDER BY created_at DESC LIMIT 256)')
        q = np.frombuffer(vector,dtype='<f4')
        ids = list(valid)
        matrix = np.stack([np.frombuffer(valid[i]['vector'],dtype='<f4') for i in ids])
        similarity = matrix @ q
        if not np.isfinite(similarity).all():
            raise RuntimeError('向量索引损坏：发现非有限相似度')
        return dict(zip(ids,map(float,similarity))), len(papers)-len(valid), bool(cached)


def load_papers(data_dir=DATA, archive=radar.LIBRARY):
    store = radar.Store(Path(data_dir)/'candidates.sqlite')
    try:
        rows = store.rows()
    finally:
        store.close()
    # Fresh archive identity check, even if a daily report was archived since the last index build.
    known = set()
    for path in (Path(archive)/'papers').glob('*/metadata.json'):
        d = json.loads(path.read_text())
        known.update((k,radar.norm_id(k,v)) for k,v in d.get('identifiers',{}).items())
    for p in rows:
        if any((k,radar.norm_id(k,v)) in known for k,v in p['identifiers'].items()):
            p['archived'] = True
    return rows


def load_notes(papers, archive=radar.LIBRARY):
    """Index current archived notes as traceable passages, not as original-paper evidence."""
    result=[]
    for paper in papers:
        if not paper.get('archive_id'):
            continue
        directory=(Path(archive)/'papers'/paper['archive_id']).resolve()
        metadata=json.loads((directory/'metadata.json').read_text())
        relative=metadata.get('artifacts',{}).get('notes')
        if not relative:
            continue
        path=(directory/relative).resolve()
        if not path.is_relative_to(directory) or path.suffix!='.md' or not path.is_file():
            continue
        lines=path.read_text().splitlines()
        heading='笔记';parts=[];first=1
        def flush(last):
            if not parts:return
            content='\n'.join(parts).strip()
            if len(content)<40:return
            key=f"{paper['id']}:note:{digest(relative)[:10]}:{first}"
            result.append({**paper,'id':key,'kind':'note','title':paper['title']+' — '+heading,
                'title_zh':(paper.get('title_zh') or paper['title'])+' · '+heading,
                'abstract':content,'archived':True,'note_location':{'file':'papers/'+paper['archive_id']+'/'+relative,
                    'heading':heading,'line_start':first,'line_end':last},
                'note_evidence':'AI精读笔记；原文结论仍须回到笔记所引的PDF页码核对。'})
        for number,line in enumerate(lines,1):
            if line.startswith('#') or sum(map(len,parts))+len(line)>950:
                flush(number-1);parts=[];first=number
                if line.startswith('#'):heading=line.lstrip('#').strip()
            parts.append(line)
        flush(len(lines))
    return result


def search(index, query, papers, mode='hybrid', limit=12, scope='candidates', source='', topic='', coverage='abstracts'):
    start = time.monotonic()
    query = query.strip()
    if not query or len(query)>1000:
        raise ValueError('请输入1–1000个字符的问题')
    if mode not in ('hybrid','semantic','keyword') or scope not in ('candidates','archive','all','notes'):
        raise ValueError('未知检索方式或范围')
    if coverage not in ('abstracts','all'):
        raise ValueError('未知摘要范围')
    catalog_count = len(papers)
    papers = [p for p in papers if (coverage=='all' or bool(p.get('abstract'))) and ((scope=='notes' and p.get('kind')=='note') or (scope!='notes' and p.get('kind')!='note' and (scope=='all' or bool(p.get('archived'))==(scope=='archive'))))
              and (not source or any(s['source_id']==source for s in p['sources']))
              and (not topic or any(t['id']==topic for t in p['topics']))]
    lexical = lexical_scores(query,papers)
    dense, missing, cache_hit, reason = {},len(papers),False,None
    effective = mode
    if mode != 'keyword' and papers:
        try:
            dense,missing,cache_hit = index.dense_scores(query,papers)
        except Exception as exc:
            effective = 'keyword'
            reason = type(exc).__name__+': '+str(exc)[:240]
    lex_order = sorted(lexical,key=lambda i:(-lexical[i],i))
    dense_order = sorted(dense,key=lambda i:(-dense[i],i))
    lex_rank = {pid:i+1 for i,pid in enumerate(lex_order)}
    dense_rank = {pid:i+1 for i,pid in enumerate(dense_order)}
    if effective=='keyword':
        scores = lexical
    elif effective=='semantic':
        scores = dense
    else:
        # Semantic-led rank fusion. Explicit heuristic weights, not learned utility/confidence.
        scores = {pid:(.2/(60+lex_rank[pid]) if pid in lex_rank else 0)+(.8/(60+dense_rank[pid]) if pid in dense_rank else 0)
                  for pid in lexical.keys() | dense.keys()}
    order = sorted(scores,key=lambda i:(-scores[i],i))[:max(1,min(limit,100))]
    lookup = {p['id']:p for p in papers}
    results = []
    for pid in order:
        p = dict(lookup[pid])
        p['retrieval'] = {'score':round(scores[pid],6),'cosine':round(dense[pid],5) if pid in dense else None,
            'keyword_score':round(lexical.get(pid,0),5),'semantic_rank':dense_rank.get(pid),'keyword_rank':lex_rank.get(pid),
            'text_basis':'title_and_abstract' if p.get('abstract') else 'title_only'}
        results.append(p)
    return {'query':query,'requested_mode':mode,'effective_mode':effective,'fallback_reason':reason,
        'model':SPEC['repo'] if dense else None,'query_cache_hit':cache_hit,'eligible':len(papers),'coverage':coverage,'catalog_size':catalog_count,
        'unindexed':missing if mode!='keyword' else None,'results':results,'elapsed_seconds':round(time.monotonic()-start,3),
        'notice':'相似度只表示检索相关性，不是学习价值或推荐置信度；入选前核对原文。'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['index','query'])
    p.add_argument('query',nargs='?')
    p.add_argument('--data-dir',type=Path,default=DATA)
    p.add_argument('--mode',choices=['hybrid','semantic','keyword'],default='hybrid')
    p.add_argument('--scope',choices=['candidates','archive','all','notes'],default='candidates')
    p.add_argument('--coverage',choices=['abstracts','all'],default='abstracts')
    p.add_argument('--source',default='');p.add_argument('--topic',default='')
    p.add_argument('--limit',type=int,default=12)
    args=p.parse_args()
    idx=Index(args.data_dir/'vectors.sqlite')
    try:
        if args.command=='index':
            archive_store=radar.Store(args.data_dir/'candidates.sqlite')
            try:radar.import_archive(archive_store,radar.LIBRARY)
            finally:archive_store.close()
        papers=load_papers(args.data_dir)
        if args.command=='index':
            notes=load_notes(papers)
            result=idx.update(papers+notes)
            result.update(paper_count=len(papers),note_passages=len(notes))
            radar.dump(args.data_dir/'semantic-status.json',result)
        else:
            documents=load_notes(papers) if args.scope=='notes' else papers
            result=search(idx,args.query or '',documents,mode=args.mode,scope=args.scope,limit=args.limit,source=args.source,topic=args.topic,coverage=args.coverage)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:
        idx.close()


if __name__=='__main__':
    main()
