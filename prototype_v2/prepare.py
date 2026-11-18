#!/usr/bin/env python3
"""Optional daily selection preflight; full reading and all 1.0 deliverables stay mandatory."""
import argparse
from contextlib import redirect_stdout
import io
import json
import subprocess
import sys
import time
import radar
from enrich import enrich
import semantic as sem
import harvest


def run(offline=False):
    start=time.monotonic()
    config=json.loads((radar.HERE/'sources.json').read_text())
    store=radar.Store(sem.DATA/'candidates.sqlite')
    warnings=[]
    try:
        radar.import_archive(store,radar.LIBRARY)
        fetcher=radar.Fetcher(sem.DATA/'cache',config,offline=offline)
        discovery=harvest.collect(store,fetcher,json.loads((radar.HERE/'discovery.json').read_text()),seconds=120)
        with redirect_stdout(io.StringIO()):
            reports=radar.collect(store,fetcher,config)+discovery
        radar.dump(sem.DATA/'sources_status.json',reports)
        enrichment=enrich(store,fetcher,limit=12,seconds=25)
        radar.dump(sem.DATA/'enrichment.json',enrichment)
        data=radar.snapshot(store,reports)
        radar.dump(radar.HERE/'candidates.json',data)
        radar.build_page(data,radar.HERE/'index.html')
    finally:store.close()
    rows=sem.load_papers()
    index=sem.Index()
    try:
        try:
            notes=sem.load_notes(rows)
            status=index.update(sorted(rows,key=lambda p:(not bool(p.get('abstract')),-p.get('score',0)))+notes,limit=3000,seconds=45)
            status.update(paper_count=len(rows),note_passages=len(notes))
            radar.dump(sem.DATA/'semantic-status.json',status)
        except Exception as exc:
            warnings.append('索引更新未完成：'+str(exc)[:200])
        queries=json.loads((radar.HERE/'research_queries.json').read_text())['queries'][:4]
        results=[sem.search(index,q,rows,limit=5) for q in queries]
        # A separate exploration lane lets previously untagged titles become abstract-backed
        # candidates, without comparing their raw cosine scores to complete abstracts.
        title_only=[p for p in rows if not p.get('abstract')]
        exploration=[sem.search(index,q,title_only,coverage='all',limit=2) for q in queries] if title_only else []
        selected={}
        for result in exploration+results:
            if result['fallback_reason']:warnings.append(result['fallback_reason'])
            for p in result['results']:
                if p['id'] not in selected:
                    selected[p['id']]={**p,'retrieved_for':[]}
                selected[p['id']]['retrieved_for'].append(result['query'])
        # Enrich shortlisted missing publisher abstracts, then refresh vectors and rerank once.
        store=radar.Store(sem.DATA/'candidates.sqlite')
        try:
            class Shortlist:
                def rows(self):return list(selected.values())
                def upsert(self,*a):return store.upsert(*a)
            more=enrich(Shortlist(),fetcher,limit=8,seconds=30)
            if any(x['status']=='ok' for x in more):
                rows=sem.load_papers()
                try:
                    notes=sem.load_notes(rows)
                    status=index.update(rows+notes,limit=50,seconds=20)
                    status.update(paper_count=len(rows),note_passages=len(notes))
                    radar.dump(sem.DATA/'semantic-status.json',status)
                except Exception as exc:warnings.append('摘要补齐后向量更新失败：'+str(exc)[:150])
                results=[sem.search(index,q,rows,limit=5) for q in queries]
                selected={}
                for result in results:
                    for p in result['results']:
                        if p['id'] not in selected:selected[p['id']]={**p,'retrieved_for':[]}
                        selected[p['id']]['retrieved_for'].append(result['query'])
            data=radar.snapshot(store,reports)
            radar.dump(radar.HERE/'candidates.json',data);radar.build_page(data,radar.HERE/'index.html')
        finally:store.close()
        shortlist=list(selected.values())[:12]
        payload={'at':radar.now(),'status':'candidate_shortlist_only','selected_paper':None,'requires_fulltext_review':True,
            'queries':queries,'papers':shortlist,'source_errors':[r for r in reports if r['status']=='error'],
            'source_fallbacks':[r for r in reports if r.get('fallback_used')],
            'source_pending':[r for r in reports if r['status']=='pending' or r.get('coverage')=='partial'],
            'title_exploration_ids':[p['id'] for result in exploration for p in result['results']],
            'warnings':warnings,'effective_modes':[r['effective_mode'] for r in results],
            'corpus_document_characters':sum(len(sem.document(p)) for p in rows),
            'shortlist_document_characters':sum(len(sem.document(p)) for p in shortlist),
            'elapsed_seconds':round(time.monotonic()-start,3)}
        radar.dump(sem.DATA/'daily-shortlist.json',payload)
        lines=['# 本轮选文线索','',f"生成时间：{payload['at']}",'','这不是最终推荐。读取最新背景与反馈，核验全文，再判断最有阅读价值的一篇；保留1.0完整汇报。',
            '',f"检索方式：{', '.join(payload['effective_modes'])}；候选语料 {len(rows)} 篇；压缩为 {len(shortlist)} 条线索。当前来源有 {len(payload['source_errors'])} 项失败、{len(payload['source_fallbacks'])} 项使用备用来源；见JSON详情。"]
        for p in shortlist:
            lines.extend(['',f"## {p.get('title_zh') or p['title']}",p.get('url') or '',
                '标识：'+json.dumps(p['identifiers'],ensure_ascii=False),'检索问题：'+'；'.join(p['retrieved_for']),
                '摘要：'+(p.get('abstract') or '缺失，当前只有标题依据。')])
        (sem.DATA/'daily-shortlist.md').write_text('\n'.join(lines)+'\n')
        return payload
    finally:index.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--offline',action='store_true');args=parser.parse_args()
    try:
        result=run(args.offline)
        print(json.dumps({k:v for k,v in result.items() if k not in ('papers','source_errors')},ensure_ascii=False))
    except Exception as exc:
        radar.dump(sem.DATA/'prepare-error.json',{'at':radar.now(),'error':type(exc).__name__+': '+str(exc)[:300],
            'next_step':'继续既有公开检索与完整精读；不可把旧shortlist当作本次新结果。'})
        print('选文辅助失败，请继续既有精读流程：'+str(exc)[:200],file=sys.stderr)
        raise SystemExit(2)
