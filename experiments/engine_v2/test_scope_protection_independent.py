"""Frozen fictional holdout: first evaluated after guard implementation.

No saved real-news titles, no API/AI labels. These are safety expectations, not
real-news accuracy measurements. Do not tune guard to this file's results.
"""
import pathlib,sys,unittest
sys.path.insert(0,str(pathlib.Path(__file__).parent))
import pre_ai as p
IMPORTANT=[
 ('架空J2クラブの最新順位表と役員の横領疑惑',''),
 ('架空カフェの限定ランチが人気','工場でアレルゲンの表示が違っていたと発表。'),
 ('架空グッズが先行発売','使用後の発煙で自主回収を決定。'),
 ('架空映画祭開催','運営元が会社更生法の適用を申請。'),
 ('架空写真展が開幕','天井が落下し来場者が救急搬送された。'),
 ('架空リーグの試合結果','従業員への給与支給が止まっている。'),
 ('架空店が新作ケーキを発売','社員に手当が支給されていなかった。'),
 ('架空コンサート開催','市の補助金の使い道に疑問が出ている。'),
 ('架空花火開催案内','橋の老朽化を受けて通行止めを実施。'),
 ('架空サッカーリーグ試合結果と運営団体の資金流用',''),
 ('架空メーカーの新商品紹介と製品安全基準の変更',''),
 ('架空写真展の開催日に建物を改修へ',''),
 ('架空カフェ新メニュー販売の裏で労災認定',''),
 ('架空リーグ順位表と役員の贈賄容疑',''),
 ('架空コンサート開催が決定','会場の不適切な管理を市が行政処分。'),
 ('架空J2リーグ試合結果','新設した運営会社が事業を分割。'),
 ('架空店の新作ケーキ限定販売と残業代の不払い',''),
 ('架空の写真展開催','会場が避難所となるため休止を検討。'),
 ('架空メーカーの新作グッズと放射性物質による汚染',''),
 ('架空コンサート開催','学校が性加害について謝罪した。'),
 ('架空リーグ試合結果','選手の自死について第三者が検証する。'),
 ('架空写真展開催','旧公共施設の移転計画を審議会へ提出。'),
 ('架空ワインフェア開催','事業者が民事再生を申し立てた。'),
 ('架空J2リーグ試合結果','運営幹部が文書を偽造したという。'),
 ('架空店の新作ケーキ紹介','勤務時間を強制的に延長したとの声。'),
 ('架空コンサートの通常開催を紹介','一方、周辺で大雪による停電が続く。'),
 ('架空写真展を開催','作品購入時の消費税制度が変更された。'),
 ('架空グッズ新作販売','商品の健康被害を調べる行政機関が発表。'),
 ('架空サッカー順位表を掲載','住民が公共施設利用をめぐり訴訟を提起。'),
 ('架空カフェの限定ランチ','ただし運営会社の巨額損失も判明した。'),
]
UNCERTAIN=[
 ('架空J2リーグ試合結果','不正は確認されていないと主張した。'),
 ('架空グッズ限定販売','対象製品はリコール対象ではないと説明。'),
 ('架空写真展開催、作中で架空の殺人事件を描く',''),
 ('架空花火開催','避難訓練で事故を想定した演習も実施。'),
 ('架空リーグ試合結果、役員の説明には不自然な点がある',''),
 ('架空写真展開催','その他のニュースとして別件を紹介。'),
 ('架空J2リーグ試合結果、主催者から別の案内もある',''),
 ('架空J2リーグ試合結果','安全性を確認できないという。'),
 ('架空コンサート開催','混乱の事情は不明とされている。'),
 ('架空グッズ販売','詳細不明で追加調査が必要。'),
]
ORDINARY=[
 '架空J2リーグ 試合結果を公表し勝利を祝う',
 '架空サッカーリーグの最新順位表',
 '架空野球 試合結果は無失点勝利',
 '架空テニス大会で優勝したチームを紹介',
 '架空店の新作ケーキを限定販売',
 '架空店 新メニューのパンケーキが登場',
 '架空メーカー グッズ発売のお知らせ',
 '架空町 写真展を開催中',
 '架空ギャラリー 浮世絵師展を20日まで展示',
 '藤井風のカフェ訪問ショットを紹介',
]
class Independent(unittest.TestCase):pass
def important(title,desc,exact=None):
 def test(self):
  result=p.classify({'url':'https://example.test/holdout','title':title,'description':desc})
  if exact:self.assertEqual(result['status'],exact)
  else:self.assertNotEqual(result['status'],'outside_candidate')
 return test
for label,rows in [('important',IMPORTANT),('uncertain',UNCERTAIN)]:
 for i,(title,desc) in enumerate(rows):setattr(Independent,'test_'+label+'_'+str(i),important(title,desc))
for i,title in enumerate(ORDINARY):setattr(Independent,'test_ordinary_'+str(i),important(title,'','outside_candidate'))
if __name__=='__main__':unittest.main()
