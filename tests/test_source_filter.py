import copy
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from source_filter import parse_rss, prefilter, measure, record_exclusions, headline
from ai_candidates import build_candidates


TITLE = '札幌市東区の共同住宅で火災、70代女性を救出し病院へ搬送、警察が原因を調査'


def article(endpoint='https://www.htb.co.jp', name='HTB北海道テレビ', title=TITLE, url='original', published='Thu, 08 Oct 2026 11:00:00 GMT'):
    return {'title': title+' - '+name, 'url': url, 'published': published,
            'rss_source': {'name': name, 'url': endpoint}}


class SourceFilterTests(unittest.TestCase):
    def test_rss_retains_source_not_google_url(self):
        root = ET.fromstring('<rss><channel><item><title>記事</title><link>https://news.google.com/rss/articles/abc</link><source url="https://www.htb.co.jp">HTB北海道テレビ</source></item></channel></rss>')
        self.assertEqual(parse_rss(root)[0]['rss_source']['url'], 'https://www.htb.co.jp')

    def test_yahoo_reprint_original_first_even_input_reversed(self):
        original = article()
        portal = article('https://news.yahoo.co.jp', 'Yahoo!ニュース', url='reprint')
        raw = [portal, original]; snapshot = copy.deepcopy(raw)
        kept, exclusions = prefilter(raw)
        self.assertEqual([a['url'] for a in kept], ['original'])
        self.assertEqual(exclusions[0]['representative_url'], 'original')
        self.assertEqual(raw, snapshot)
        self.assertFalse(exclusions[0]['producer_verified'])

    def test_credited_reprint(self):
        portal = article('https://news.yahoo.co.jp', 'Yahoo!ニュース', title=TITLE+'（HTB北海道ニュース）', url='reprint')
        kept, excluded = prefilter([portal, article()])
        self.assertEqual(len(kept), 1)
        self.assertTrue(excluded[0]['producer_verified'])

    def test_conflicting_producer_is_not_removed(self):
        portal = article('https://news.yahoo.co.jp', 'Yahoo!ニュース', title=TITLE+'（北海道ニュースUHB）', url='reprint')
        self.assertEqual(len(prefilter([portal, article()])[0]), 2)

    def test_yahoo_original_remains_even_matching_headline(self):
        portal = article('https://news.yahoo.co.jp', 'Yahoo!ニュース オリジナル', url='original_yahoo')
        kept, excluded = prefilter([portal, article()])
        self.assertFalse(excluded)
        self.assertEqual(kept[0]['collection_role'], 'yahoo_original')

    def test_unavailable_original_is_supplementary(self):
        portal = article('https://news.yahoo.co.jp', 'Yahoo!ニュース', url='important')
        kept, excluded = prefilter([portal])
        self.assertFalse(excluded)
        self.assertEqual(kept[0]['collection_role'], 'supplementary')

    def test_no_source_does_not_use_google_url(self):
        unknown = {'title': TITLE, 'url': 'https://news.google.com/rss/articles/abc', 'published': article()['published']}
        self.assertEqual(prefilter([unknown])[0][0]['collection_role'], 'source_unconfirmed')

    def test_independent_accident_and_mice_baseball_remain(self):
        for other in ['札幌市北区の単独事故で40代男性を搬送', '札幌支部の北海、足寄下す 秋の高校野球・全道大会']:
            kept, excluded = prefilter([article(title='札幌MICE基本計画の費用に議会で懸念'), article('https://news.yahoo.co.jp', 'Yahoo!ニュース', title=other, url='other')])
            self.assertEqual(len(kept), 2)
            self.assertFalse(excluded)

    def test_number_or_stage_changes_remain(self):
        for changed in [TITLE.replace('70代', '80代'), TITLE.replace('救出', '死亡確認'), TITLE.replace('東区', '北区')]:
            self.assertEqual(len(prefilter([article(), article('https://news.yahoo.co.jp', 'Yahoo!ニュース', title=changed, url='followup')])[0]), 2)

    def test_body_update_same_headline_remains(self):
        original = dict(article(), content='女性が救出された')
        newer = dict(article('https://news.yahoo.co.jp', 'Yahoo!ニュース', url='updated'), content='搬送先で死亡が確認された')
        self.assertEqual(len(prefilter([original, newer])[0]), 2)

    def test_post_grouping_does_not_undo_safe_followup_selection(self):
        from news_deduplicate import deduplicate_groups
        original = article()
        followup = article('https://news.yahoo.co.jp', 'Yahoo!ニュース', title=TITLE.replace('救出', '死亡確認'), url='followup')
        groups, exclusions = deduplicate_groups([{'event_name': '火災', 'articles': [original, followup]}])
        self.assertEqual(len(groups[0]['articles']), 2)
        self.assertFalse(exclusions)

    def test_missing_date_or_distant_date_remains(self):
        for date in ['', 'Sat, 10 Oct 2026 11:00:00 GMT']:
            self.assertEqual(len(prefilter([article(), article('https://news.yahoo.co.jp', 'Yahoo!ニュース', url='other', published=date)])[0]), 2)

    def test_short_generic_headline_remains(self):
        self.assertEqual(len(prefilter([article(title='札幌で火災'), article('https://news.yahoo.co.jp', 'Yahoo!ニュース', title='札幌で火災', url='other')])[0]), 2)

    def test_same_endpoint_duplicates_and_exact_url(self):
        kept, excluded = prefilter([article(), article(url='same_source_copy')])
        self.assertEqual(len(kept), 1)
        self.assertEqual(excluded[0]['reason'], 'same_endpoint_exact_headline')
        self.assertEqual(len(prefilter([article(), article()])[0]), 1)

    def test_two_independent_broadcasters_are_not_deduplicated(self):
        self.assertEqual(len(prefilter([article(), article('https://www.uhb.jp', 'UHB 北海道文化放送', url='uhb')])[0]), 2)

    def test_cost_cap_is_not_claimed_as_savings(self):
        raw = [article(url=str(i), title=TITLE+str(i)) for i in range(20)]
        stats = measure(raw, raw, [])
        self.assertEqual(stats['estimated_first_stage_calls_saved'], 0)
        self.assertLessEqual(stats['first_stage_call_limit_after'], 100)

    def test_exclusions_append_and_preserve_articles(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp)/'archive.json')
            _, excluded = prefilter([article(), article('https://news.yahoo.co.jp', 'Yahoo!ニュース', url='yahoo')])
            record_exclusions(excluded, 'rss', path)
            record_exclusions(excluded, 'rss', path)
            record_exclusions(excluded, 'lookup', path)
            archived = json.loads(Path(path).read_text())
            self.assertEqual(len(archived), 2)
            self.assertEqual(archived[0]['article']['url'], 'yahoo')

    def test_lookup_uses_policy_before_ai(self):
        import tracking_sources
        xml = '<rss><channel><item><title>'+TITLE+' - Yahoo!ニュース</title><link>yahoo</link><pubDate>Thu, 08 Oct 2026 11:00:00 GMT</pubDate><source url="https://news.yahoo.co.jp">Yahoo!ニュース</source></item><item><title>'+TITLE+' - HTB北海道テレビ</title><link>htb</link><pubDate>Thu, 08 Oct 2026 11:00:00 GMT</pubDate><source url="https://www.htb.co.jp">HTB北海道テレビ</source></item></channel></rss>'
        with tempfile.TemporaryDirectory() as temp:
            import io
            with patch('tracking_sources.urllib.request.urlopen', return_value=io.BytesIO(xml.encode())), patch('source_filter.record_exclusions') as record:
                self.assertEqual([a['url'] for a in tracking_sources.search('test')], ['htb'])
                record.assert_called_once()

    def test_unpaired_supplementary_article_enters_pending_pipeline(self):
        import news_ai_grouping
        with tempfile.TemporaryDirectory() as temp:
            old = os.getcwd()
            try:
                os.chdir(temp)
                Path('ai_results.json').write_text('[]')
                Path('news_filtered.json').write_text(json.dumps([article('https://news.yahoo.co.jp', 'Yahoo!ニュース', url='unique')]))
                news_ai_grouping.main()
                self.assertEqual(json.loads(Path('news_groups_ai.json').read_text())[0]['articles'][0]['url'], 'unique')
            finally:
                os.chdir(old)


if __name__ == '__main__':
    unittest.main()
