"""Fictional regressions for the previously failed twenty cases and context."""
import copy
import pathlib
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).parent))
import pre_ai as p
import scope_protection as guard
from test_pre_ai import article,group,NOW
CASES = ['架空サッカー 試合結果を発表、運営幹部の収賄も判明', '架空リーグ 最新順位表、運営会社の粉飾決算も報道', '架空グッズ 新商品発売後にリコールを発表', '架空の新作ケーキ、アレルゲン誤表示が判明', '架空歌手のライブ開催決定、所属団体のハラスメント問題も報道', '架空俳優のプライベート写真公開、肖像利用で訴訟に', '架空写真展の開催案内、会場の汚職問題も記載', '架空映画祭が開幕、委託業者の談合が明らかに', '架空J2リーグ 試合結果、観客の行方不明事案にも言及', '架空ワインの限定販売、原料の産地偽装が発覚', '架空コンサート開催決定、会場の爆破予告で警戒', '架空カフェ 人気ランチ、従業員への賃金未払いが判明', '架空マラソン 結果速報、運営団体の会計不正を告発', '架空高校駅伝で優勝、選手へのパワハラを学校が調査', '架空居酒屋 人気メニュー、許認可取り消しへ行政指導', '架空スポーツ団体の最新順位表と経営破綻について', '架空菓子店 新作ケーキ限定発売、工場から有害物質', '架空グッズ 新商品販売、全国で発煙事案が相次ぐ', '架空ギャラリー 企画展開催、天井落下で来場者を搬送', '架空カフェの新作ケーキ、会社更生法の適用を申請']
class HistoricalTwenty(unittest.TestCase):pass
def regression(title):
 def test(self):self.assertNotEqual(p.classify(article(title=title))['status'],'outside_candidate')
 return test
for i,title in enumerate(CASES):setattr(HistoricalTwenty,'test_case_'+str(i),regression(title))
class Context(unittest.TestCase):
 def test_negated_accident_held(self):
  r=p.classify(article(title='架空グッズ発売、事故はなかった'));self.assertEqual(r['status'],'hold')
 def test_denial_not_cleared(self):
  self.assertEqual(p.classify(article(title='架空リーグ順位表、運営は収賄を否定'))['status'],'hold')
 def test_positive_after_denial_wins(self):
  r=p.classify(article(title='架空リーグは事故なしと説明。しかし幹部を収賄容疑で逮捕'));self.assertEqual(r['status'],'inside')
 def test_fiction_not_fact_or_exclusion(self):
  r=p.classify(article(title='新作ゲームが発売、作中の犯罪を描く'));self.assertEqual(r['status'],'hold')
 def test_training_not_actual_disaster(self):
  r=p.classify(article(title='架空地区のイベント、火災を想定訓練'));self.assertEqual(r['status'],'hold')
 def test_mixed_unknown_topic_held(self):
  self.assertEqual(p.classify(article(title='架空J2リーグ試合結果、運営元から別の説明がある'))['status'],'hold')
 def test_rss_risk_wins_plain_title(self):
  r=p.classify(article(description='<p>給与が支払われていない。</p>'));self.assertEqual(r['status'],'inside')
 def test_rss_negation_not_permission(self):
  self.assertEqual(p.classify(article(description='リコール対象ではないと説明'))['status'],'hold')
 def test_important_rss_after_title_denial(self):
  r=p.classify(article(title='架空リーグ、不正は確認されず',description='市議会が予算を変更した'));self.assertEqual(r['status'],'inside')
 def test_uncertainty_not_promoted(self):
  self.assertEqual(p.classify(article(title='架空リーグ試合結果、事情は不明で問題が残る'))['status'],'hold')
 def test_rss_html_href_not_an_incident(self):
  a=article(description='<a href="https://example.test/収賄">試合結果 勝利</a>');self.assertEqual(p.classify(a)['status'],'outside_candidate')
 def test_source_bound_protection_receipt(self):
  r=p.classify(article(description='<p>リコールを発表。</p>'));self.assertTrue(any(x['field']=='rss_description' for x in r['protection_contexts']))
 def test_rule_changes_when_guard_changes(self):
  old=p.rule_id()
  with patch.object(guard,'revision',return_value='fictional-next-guard'):
   self.assertNotEqual(old,p.rule_id())
 def test_article_update_rechecks_guard(self):
  a=article();b={**a,'description':'製品の自主回収を発表'};self.assertNotEqual(p.classify(a)['article_version'],p.classify(b)['article_version']);self.assertEqual(p.classify(b)['status'],'inside')
 def test_original_metadata_not_normalized(self):
  a=article();before=copy.deepcopy(a);p.classify(a);self.assertEqual(a,before)
 def test_receipt_replay_rejects_guard_change(self):
  db=p.open_store();g=[group(article())];p.save_plan(db,'op',g,NOW)
  with patch.object(guard,'revision',return_value='fictional-next-guard'):
   with self.assertRaises(ValueError):p.save_plan(db,'op',g,NOW)
  db.close()
 def test_ordinary_sports_still_candidate(self):self.assertEqual(p.classify(article())['status'],'outside_candidate')
 def test_ordinary_product_still_candidate(self):self.assertEqual(p.classify(article(title='架空店 新作ケーキを限定販売'))['status'],'outside_candidate')
 def test_ordinary_event_still_candidate(self):self.assertEqual(p.classify(article(title='架空写真展 開催中'))['status'],'outside_candidate')
 def test_ordinary_celebrity_still_candidate(self):self.assertEqual(p.classify(article(title='藤井風 カフェでのショット'))['status'],'outside_candidate')
if __name__=='__main__':unittest.main()
