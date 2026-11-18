import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
import reading_journal as journal


class ReadingJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        for pid in ('paper-a','paper-b'):
            p=self.root/'papers'/pid;p.mkdir(parents=True)
            (p/'metadata.json').write_text(json.dumps({'title':pid,'title_zh':'论文'+pid}))
        self.original = ('# 每日一句\n\n| 日期 | 论文 | AI候选句 | 我的最终句 | 阅读笔记 |\n'
            '|---|---|---|---|---|\n'
            '| 2026-11-17 | Paper A | AI 的参考 | 原来的一句 | [笔记](papers/paper-a/notes.html) |\n'
            '| 2026-11-18 | Paper B | 另一条参考 | 别动这一句 | [笔记](papers/paper-b/notes.html) |\n\n保留这些说明。\n')
        self.path=self.root/'reading_log.md';self.path.write_text(self.original)

    def tearDown(self):self.tmp.cleanup()

    def request(self, original='我自己的理解'):
        row=journal.entries(self.root)[0]
        return journal.create_request(row['paper_id'],row['date'],original,row['revision'],self.root)

    def test_only_confirm_changes_final_and_preserves_other_content(self):
        request=self.request()
        self.assertEqual(self.path.read_text(),self.original)
        with self.assertRaises(journal.Conflict):
            journal.confirm(request['id'],'不能提前写入',request['revision'],self.root)
        result=journal.suggest(request['id'],'一版润色稿','只调整了语序',self.root)
        self.assertEqual(self.path.read_text(),self.original)
        final='修订 | 结果\\符号\n<这里不是HTML> & 测试'
        saved=journal.confirm(request['id'],final,result['revision'],self.root)
        self.assertEqual(journal.entries(self.root)[0]['final'],final)
        self.assertEqual(journal.entries(self.root)[1]['final'],'别动这一句')
        self.assertIn('AI 的参考',self.path.read_text());self.assertIn('保留这些说明。',self.path.read_text())
        self.assertEqual(saved['original'],'我自己的理解')
        self.assertEqual(saved['suggestion'],'一版润色稿')
        self.assertEqual((self.root/'.reading-journal/history'/f"{request['id']}.before.md").read_text(),self.original)
        self.assertEqual(journal.confirm(request['id'],final,result['revision'],self.root)['status'],'confirmed')

    def test_external_edit_is_not_overwritten(self):
        request=self.request();ready=journal.suggest(request['id'],'润色','说明',self.root)
        self.path.write_text(self.original.replace('原来的一句','我在编辑器里改了'))
        with self.assertRaises(journal.Conflict):journal.confirm(request['id'],'润色',ready['revision'],self.root)
        self.assertIn('我在编辑器里改了',self.path.read_text())

    def test_direct_save_preserves_words_other_rows_and_backup(self):
        row=journal.entries(self.root)[0]
        original='我的总结 | 保留\\符号\n<条件> & 不确定性'
        saved=journal.save_original(row['paper_id'],row['date'],original,row['revision'],self.root)
        self.assertEqual(saved['status'],'confirmed')
        self.assertEqual(saved['mode'],'direct')
        self.assertEqual(saved['original'],original)
        self.assertEqual(saved['final'],original)
        self.assertIsNone(saved['suggestion'])
        self.assertEqual(journal.entries(self.root)[0]['final'],original)
        self.assertEqual(journal.entries(self.root)[1]['final'],'别动这一句')
        self.assertIn('AI 的参考',self.path.read_text())
        self.assertIn('保留这些说明。',self.path.read_text())
        self.assertEqual((self.root/'.reading-journal/history'/f"{saved['id']}.before.md").read_text(),self.original)
        self.assertEqual(journal.entries(self.root)[0]['latest_request'],saved['id'])

    def test_direct_save_rejects_stale_row_and_does_not_leave_request(self):
        row=journal.entries(self.root)[0]
        changed=self.original.replace('原来的一句','我在其他窗口新写的')
        self.path.write_text(changed)
        with self.assertRaises(journal.Conflict):
            journal.save_original(row['paper_id'],row['date'],'旧页面文字',row['revision'],self.root)
        self.assertEqual(self.path.read_text(),changed)
        self.assertFalse(list((self.root/'.reading-journal/requests').glob('*.json')))

    def test_direct_save_while_ai_pending_cannot_be_overwritten_by_late_proposal(self):
        pending=self.request()
        row=journal.entries(self.root)[0]
        saved=journal.save_original(row['paper_id'],row['date'],pending['original'],row['revision'],self.root)
        late=journal.suggest(pending['id'],'迟到的建议','解释',self.root)
        with self.assertRaises(journal.Conflict):
            journal.confirm(late['id'],late['suggestion'],late['revision'],self.root)
        self.assertEqual(journal.entries(self.root)[0]['final'],pending['original'])
        self.assertEqual(journal.entries(self.root)[0]['latest_request'],saved['id'])

    def test_direct_save_rejects_empty_or_invalid_text(self):
        row=journal.entries(self.root)[0]
        for original in ('', '   ', None, 'x'*4001, '含\0空字符'):
            with self.subTest(original=repr(original)[:30]), self.assertRaises(ValueError):
                journal.save_original(row['paper_id'],row['date'],original,row['revision'],self.root)
        self.assertEqual(self.path.read_text(),self.original)

    def test_other_row_edit_is_preserved_without_blocking(self):
        request=self.request();ready=journal.suggest(request['id'],'润色','说明',self.root)
        self.path.write_text(self.original.replace('别动这一句','另一行的新文本'))
        journal.confirm(request['id'],'润色',ready['revision'],self.root)
        self.assertEqual(journal.entries(self.root)[1]['final'],'另一行的新文本')

    def test_two_requests_cannot_overwrite_each_other(self):
        first=self.request();second=self.request('新的原话')
        a=journal.suggest(first['id'],'版本甲','说明',self.root)
        b=journal.suggest(second['id'],'版本乙','说明',self.root)
        journal.confirm(first['id'],'版本甲',a['revision'],self.root)
        with self.assertRaises(journal.Conflict):journal.confirm(second['id'],'版本乙',b['revision'],self.root)

    def test_request_id_and_duplicate_row_are_rejected(self):
        with self.assertRaises(ValueError):journal.get_request('../../config',self.root)
        self.path.write_text(self.original+self.original.splitlines()[4]+'\n')
        with self.assertRaises(ValueError):self.request()

    def test_proposal_cannot_silently_change_after_review(self):
        request=self.request();ready=journal.suggest(request['id'],'版本甲','说明',self.root)
        with self.assertRaises(journal.Conflict):journal.suggest(request['id'],'版本乙','说明',self.root)
        with self.assertRaises(journal.Conflict):journal.confirm(request['id'],'版本甲',request['revision'],self.root)
        self.assertEqual(journal.get_request(request['id'],self.root)['revision'],ready['revision'])

    def test_late_old_proposal_does_not_replace_newer_request_entry(self):
        old=self.request();new=self.request('更新的原话')
        journal.suggest(old['id'],'较早原话的建议','说明',self.root)
        self.assertEqual(journal.entries(self.root)[0]['latest_request'],new['id'])

    def test_deeplink_has_workspace_and_prompt_without_auto_submit(self):
        request=self.request();url=journal.handoff(request['id'],self.root)['url']
        parsed=urlsplit(url);query=parse_qs(parsed.query)
        self.assertEqual((parsed.scheme,parsed.netloc),('codex','new'))
        self.assertEqual(query['path'],[str(self.root)])
        self.assertIn(request['id'],query['prompt'][0]);self.assertNotIn('我自己的理解',query['prompt'][0])
        self.assertEqual(set(query),{'path','prompt'})


if __name__=='__main__':unittest.main()
