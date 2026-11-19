"""Missed-delivery checks use synthetic archives; no model, network or desktop."""
from datetime import datetime, timedelta, timezone
import json
import unittest
from zoneinfo import ZoneInfo
import test_paper_notify as fixture

notify = fixture.notify

class DeliveryNoticeTests(unittest.TestCase):
    save = fixture.Notifications.save
    tearDown = fixture.Notifications.tearDown

    def setUp(self):
        fixture.Notifications.setUp(self)
        self.config = {'timezone':'Asia/Shanghai', 'desktop_notification':{
            'late_report_alert': {'enabled':True, 'after':'09:00'}}}
        self.write_config()
        self.now = datetime.fromisoformat(self.day+'T09:00:00+08:00')

    def write_config(self):
        (self.root/'config.json').write_text(json.dumps(self.config))

    def incomplete(self):
        (self.paper/self.data['artifacts']['video']).unlink()

    def notice(self, now=None, **kw):
        return notify.pending_delivery_notice(self.root, now=now or self.now, **kw)

    def test_disabled_until_configured(self):
        self.incomplete()
        (self.root/'config.json').unlink()
        self.assertIsNone(self.notice())

    def test_not_early_but_exactly_at_cutoff(self):
        self.incomplete()
        self.assertIsNone(self.notice(self.now-timedelta(seconds=1)))
        self.assertEqual(self.notice()['date'],self.day)

    def test_utc_input_is_converted_to_beijing(self):
        self.incomplete()
        self.assertEqual(self.notice(self.now.astimezone(timezone.utc))['date'], self.day)
        self.assertIsNone(self.notice((self.now-timedelta(seconds=1)).astimezone(timezone.utc)))

    def test_complete_even_when_dismissed_does_not_alert(self):
        item=notify.pending_papers(self.root)[0]
        notify.save_state(item, acknowledged_at=self.now.isoformat())
        self.assertIsNone(self.notice())

    def test_old_complete_report_cannot_satisfy_today(self):
        tomorrow=self.now+timedelta(days=1)
        self.assertEqual(self.notice(tomorrow)['date'],tomorrow.date().isoformat())

    def test_partial_report_is_not_ready(self):
        self.data['status']='partial';self.save()
        self.assertIsNotNone(self.notice())

    def test_missing_background_is_not_ready(self):
        (self.paper/'background.md').unlink()
        self.assertIsNotNone(self.notice())

    def test_display_without_close_is_recovered(self):
        self.incomplete()
        item=self.notice()
        notify.save_delivery_state(item,status='displayed',displayed_at=self.now.isoformat())
        self.assertIsNotNone(self.notice())

    def test_dismissed_notice_does_not_suppress_later_paper(self):
        item=notify.pending_papers(self.root)[0]
        receipt=item['state_path']
        self.data['status']='partial';self.save()
        notice=self.notice()
        notify.save_delivery_state(notice,acknowledged_at=self.now.isoformat())
        self.assertIsNone(self.notice())
        self.assertIsNotNone(self.notice(include_acknowledged=True))
        self.assertFalse(receipt.exists())
        self.data['status']='ready';self.save()
        self.assertIsNone(self.notice(include_acknowledged=True))
        self.assertEqual(len(notify.pending_papers(self.root)),1)

    def test_next_day_has_independent_notice(self):
        self.incomplete()
        item=self.notice()
        notify.save_delivery_state(item,acknowledged_at=self.now.isoformat())
        self.assertIsNotNone(self.notice(self.now+timedelta(days=1)))

    def test_unreadable_notice_is_retried(self):
        self.incomplete()
        item=self.notice();item['state_path'].parent.mkdir(exist_ok=True)
        item['state_path'].write_text('{unfinished')
        self.assertIsNotNone(self.notice())
        item['state_path'].write_text('[]')
        self.assertIsNotNone(self.notice())

    def test_invalid_optional_config_does_not_break_reader(self):
        for text in ('{unfinished','[]','{"timezone":"bad-zone","desktop_notification":{"late_report_alert":{"enabled":true}}}'):
            (self.root/'config.json').write_text(text)
            self.assertIsNone(self.notice())
            self.assertEqual(len(notify.available_papers(self.root)),1)

if __name__ == '__main__':
    unittest.main()
