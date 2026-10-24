"""Conservative saved-text scope guards. No assertion of reporting accuracy.

A negated, fictional or disputed incident is NOT permission to discard an article.
Protection is evaluated per clause/field before any light-topic classification.
"""
import hashlib
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
import re

class _RSS(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[]
    def handle_data(self,data):self.parts.append(data)


def fields(title,description):
    parser=_RSS()
    parser.feed(description if isinstance(description,str) else '')
    return [('title',unescape(title) if isinstance(title,str) else ''),('rss_description',' '.join(parser.parts))]


# Grouped by scope, not by entertainment/sport person names. Pairs below cover
# meaningful actions even where no single conventional incident noun is used.
FAMILIES={
 'crime_justice':re.compile(r'収賄|贈賄|汚職|談合|横領|恐喝|脅迫|爆破予告|殺害予告|背任|粉飾|偽装|偽造|改ざん|改竄|隠蔽|隠ぺい|逮捕|捜査|起訴|不起訴|訴訟|裁判|判決|無罪|容疑|検察|警察|検挙|無断|無資格|違反'),
 'accident_product_safety':re.compile(r'リコール|自主回収|回収命令|誤表示|アレルゲン|有害物質|発煙|発火|破裂|爆発|落下|倒壊|搬送|救急|救出|負傷|火災|事故|健康被害|食中毒|放射性|被ばく|汚染|有毒|中毒|欠陥|安全基準|遭難|行方不明|失踪'),
 'labor_organization':re.compile(r'ハラスメント|パワハラ|セクハラ|賃金|給与|残業代|未払い|不払い|労災|過労|労組|ストライキ|解雇|雇い止め|性加害|自殺|自死|退職強要|不祥事|不正|不適切|公益通報|内部告発|会計不正|謝罪|懲戒'),
 'administration_policy':re.compile(r'許認可|行政指導|行政処分|行政判断|是正命令|取消し|取り消し|法改正|条例|制度|規制|補助金|公金|税金|公費|入札|議決|予算|給食|自治体|審議会'),
 'disaster_infrastructure':re.compile(r'被災|災害|豪雨|豪雪|大雪|暴風|吹雪|浸水|洪水|土砂|避難|警報|噴火|震災|地震|復旧|復興|停電|断水|通行止め|運休|減便|廃線|道路陥没|脱線|耐震|老朽化|公共施設|改修|再開発|インフラ'),
 'corporate_change':re.compile(r'経営破綻|経営統合|会社更生|民事再生|破産|倒産|債務超過|負債|巨額損失|赤字|事業譲渡|買収|株式取得|TOB|上場廃止|組織再編|社名変更|新会社設立|事業撤退|事業停止|資金流用'),
}
CONTEXT_PAIRS=[
 ('labor_organization',re.compile(r'(?:従業員|職員|労働者|社員|スタッフ|手当).{0,35}(?:支給されていな|支払われていな|支払っていな|払われず|支払いを拒否|支給せず|休めな|働かせ)')),
 ('labor_organization',re.compile(r'(?:勤務|就業).{0,15}(?:時間|シフト).{0,15}(?:強制|延長|削減)')),
 ('crime_justice',re.compile(r'(?:帳簿|書類|報告書|会計).{0,20}(?:書き換え|虚偽|ごまかし|不自然)')),
 ('administration_policy',re.compile(r'(?:市|県|町|村|国|道|役所|省|庁).{0,20}(?:許可を出さ|許可しな|許可を撤|利用を制限|負担を増|利用料を変)')),
 ('corporate_change',re.compile(r'(?:会社|企業|団体|運営元).{0,25}(?:事業を分割|部門を統合|資金が尽き|資金が足り|支払いができ|資産を売却)')),
]
UNCERTAIN=re.compile(r'疑惑|疑い|懸念|おそれ|恐れ|危険|不具合|異常|問題|不安|不可解|不審|疑問|調査|調べ|検証|事案|騒動|騒ぎ|トラブル|通報|告発|批判|抗議|被害を訴え|安全性.{0,8}(?:不明|確認でき)|詳細不明|事情は不明')
FICTION=re.compile(r'劇中|作中|フィクション|(?:映画|ドラマ|小説|演劇|ゲーム).{0,25}(?:描く|物語|あらすじ)|架空の.{0,15}(?:描く|物語)|防災訓練|避難訓練|事故を想定|想定訓練')
NEGATION=re.compile(r'なし|なかった|ないと|ないこと|ではない|ではなく|認めず|否定|未確認|確認されず|確認されな|該当せず|対象外|対象ではな|誤報')
CLAUSES=re.compile(r'[。\n！？!?；;、]|しかし|一方|ただし|だが|ところが')


def revision():return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def assess(title,description,legacy):
    receipts=[];uncertain=[]
    for field,text in fields(title,description):
        for clause in CLAUSES.split(text):
            matches=[]
            for family,pattern in list(FAMILIES.items())+CONTEXT_PAIRS+[('legacy_protection',legacy)]:
                match=pattern.search(clause)
                if match:matches.append((family,match.group()))
            # Never infer a fact from a negation or a work of fiction. Preserve
            # it as hold; a positive signal in another clause still wins.
            context='fiction_or_training' if FICTION.search(clause) else 'negated_or_disputed' if NEGATION.search(clause) else 'asserted_scope_signal'
            for family,signal in matches:
                receipts.append({'field':field,'family':family,'signal':signal,'context':context})
            caution=UNCERTAIN.search(clause)
            if caution:uncertain.append({'field':field,'signal':caution.group()})
    positive=[r for r in receipts if r['context']=='asserted_scope_signal']
    if positive:return {'status':'inside','reason':'important_context_protected','signals':receipts,'cautions':uncertain}
    if receipts:return {'status':'hold','reason':'negated_fictional_or_disputed_scope','signals':receipts,'cautions':uncertain}
    if uncertain:return {'status':'hold','reason':'unresolved_risk_context','signals':[],'cautions':uncertain}
    return {'status':None,'reason':None,'signals':[],'cautions':[]}


def mixed_topic_uncertain(title,description,topic_patterns):
    """Unknown additional subjects cannot inherit an ordinary-topic exemption.

    This is conservative segmentation, not general Japanese semantic parsing.
    Source suffix stripping here NEVER changes article/representative identity.
    RSS fields repeating a headline add no independent verification.
    """
    head=title.split(' - ')[0] if isinstance(title,str) else ''
    clauses=[c.strip() for c in CLAUSES.split(head) if c.strip()]
    if len(clauses)>1 and any(len(c)>3 and not any(p.search(c) for p in topic_patterns) for c in clauses):
        return True
    rss=fields('',description)[1][1]
    if re.search(r'ほかにも|別の話題|その他のニュース|あわせて報道|複数の話題',rss):return True
    return False
