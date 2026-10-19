"""Offline, reversible queue projection. No AI, fetch, adoption or public writer.

Scope candidates are reversible deferrals, not verified exclusions. Representation
is an operator-authorized metadata policy, NOT evidence of identical events/text.
"""
import copy
import hashlib
import html
import json
import re
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

RULE_VERSION = 'pre-ai-scope-1'
JST = timezone(timedelta(hours=9))
FIELDS = ('url', 'title', 'description', 'published')
# Protection takes precedence over ALL light-topic signals. False positives are
# acceptable here: uncertain protected stories keep their processing opportunity.
PROTECT = re.compile(r'逮捕|起訴|裁判|判決|控訴|捜査|強盗|殺人|暴行|窃盗|詐欺|犯罪|事故|火災|出火|死亡|死去|重傷|負傷|けが|骨折|被害|不祥事|不適切|漏えい|漏洩|不正|破産|倒産|買収|TOB|上場廃止|再編|経営統合|社名変更|代表.*就任|廃止|閉鎖|撤去|違法|無許可|条例|制度|政策|規制|予算|公費|行政|市議会|審議会|支援|災害|地震|洪水|浸水|土砂|復旧|復興|避難|警報|減便|運休|運賃|公共|再開発|建設|整備|改修|インフラ|空港|地下鉄|路面電車|市電|交通|給食|医療|感染|食中毒|訴訟|虐待|労働|補償|中止|延期|取消|撤回|訂正|解任|税|公共施設|アリーナ|札幌ドーム|公園野球場')
SPORT = re.compile(r'コンサドーレ|ファイターズ|野球|サッカー|バスケ|バレー|ラグビー|[ＪJ][１２３123]|リーグ|レバンガ|マラソン|駅伝|テニス|ホッケー|磐田|DAZN|陸上競技|プロレス|競馬')
RESULT = re.compile(r'試合結果|順位表|最下位|勝ち点|優勝|勝利|連敗|[0-9０-９]+勝目|ゴール|得点|安打|盗塁|投球|登板|[0-9０-９]+強|[0-9０-９]+回戦|勝ち|敗れ|敗退|無敗|完封|クリーンシート|対戦|vs[.\s]|ライブ[ 　]?中継|ライブ配信|あす.*戦|きょう.*戦|戦後')
PRODUCT = re.compile(r'グッズ|新商品|新作|新メニュー|スイーツ|パンケーキ|ラーメン|ケーキ|餃子|カクテル|ディナー|ランチ|ノベルティ|ポンチョ|GORE.TEX|ゴアテックス')
PROMO = re.compile(r'販売|発売|予約|プレゼント|限定|登場|新登場|味わ|絶品|美味|おいし|極上|人気|グッズ|グルメ|オープン|紹介')
EVENT = re.compile(r'写真展|写真.*グランプリ展|浮世絵.*展|道展|サルビア展|洋ラン展|ダリア.*展示|花火|イルミネーション|クリスマス市|映画祭|音楽フェス|コンサート|トークショー|うまいもの大会|ワインフェア|ワイン.*フェア|スタンプラリー')
ANNOUNCE = re.compile(r'開催|開幕|開催中|開催決定|出演決定|展示|集結|まで|日から|日開幕|楽し|味わ|飲み比べ')
CELEB = re.compile(r'幾田りら|藤井風|長谷川雅紀|錦鯉|松岡昌宏|長嶋一茂|佐久間宣行')
LIGHT_NEWS = re.compile(r'ライブ|ショット|SNS|人気.*コンビ|後輩|寿司|サウナ|出没|カフェ')
AMBIGUOUS = re.compile(r'[＜<]ひと[＞>]|クマ|ヒグマ|シカ|動物|学校|病院|会議所|市長|市議|知事|SDGs|スポGOMI|スポＧＯＭＩ|ごみ拾い|地球|環境|文化財|道路|駅|公園|施設|臨時.*バス|シャトルバス|経営|企業|組織')


def dumps(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(x):
    return hashlib.sha256(dumps(x).encode()).hexdigest()


def rule_id():
    return RULE_VERSION + ':' + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def version_id(article):
    # Mirrors the fixed-data audit's FOUR fields; no title/source normalization.
    # Explicit body/version fields additionally invalidate receipts if supplied.
    return digest({k: article.get(k, '') for k in FIELDS} | {
        k: article[k] for k in ('article_version_id', 'body', 'content_hash') if k in article})


def _source(article):
    source = article.get('rss_source')
    value = source.get('name') if isinstance(source, dict) else None
    return value if isinstance(value, str) and value.strip() and value.strip() not in {'unknown', '未確認', '不明'} else None


def jst_day(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    if dt is None or dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt.astimezone(JST).date().isoformat()


def classify(article):
    title = article.get('title')
    desc = article.get('description', '')
    # HTML tags removed ONLY for scope screening; strict grouping uses originals.
    text = html.unescape(re.sub(r'<[^>]*>', ' ', (title if isinstance(title, str) else '') + '\n' + (desc if isinstance(desc, str) else '')))
    protected = PROTECT.search(text)
    missing = not isinstance(title, str) or not title.strip()
    candidate = None
    signals = [protected.group()] if protected else []
    if not missing and not protected and not AMBIGUOUS.search(text):
        for subtype, topic, context in [('sports', SPORT, RESULT), ('product', PRODUCT, PROMO),
                                         ('event', EVENT, ANNOUNCE), ('celebrity', CELEB, LIGHT_NEWS)]:
            if topic.search(text) and context.search(text):
                candidate = subtype
                signals = [topic.search(text).group(), context.search(text).group()]
                break
    # A token is a conservative protection signal, not proof of factual accuracy.
    status = 'hold' if missing else 'inside' if protected else 'outside_candidate' if candidate else 'hold'
    return {'status': status, 'subtype': candidate, 'protected': bool(protected),
            'reason': 'missing_title' if missing else 'protected_scope_signal' if protected else 'topic_and_context' if candidate else 'scope_unconfirmed',
            'basis': 'saved_headline_and_rss_only', 'body_verified': False,
            'matched_signals': signals,
            'article_version': version_id(article), 'rule_id': rule_id(),
            'recheck_on': ['article_revision', 'rss_source_change', 'rule_change', 'operator_override', 'new_evidence'],
            'safe_exclusion_confirmed': False}


def _separate(left, right, event_bindings, distinct_pairs):
    if frozenset((left, right)) in distinct_pairs:
        return True
    a, b = set(event_bindings.get(left, [])), set(event_bindings.get(right, []))
    # Different saved event assignments conservatively block representation.
    # Shared articles may keep multiple IDs: representation never merges them.
    return bool(a and b and a != b)


def plan(groups, *, separate_pairs=(), event_bindings=None, disabled_cohorts=(), overrides=None):
    """Return an isolated work projection; inputs and their original queues survive.

    All identity/quote/decision/validation downstream consumers MUST use original
    versions, not interpret an alias as validated evidence or an AI-cache hit.
    Caller supplies known event assignments/distinct operator judgments.
    """
    groups = copy.deepcopy(groups)
    bindings = copy.deepcopy(event_bindings or {})
    overrides = overrides or {}
    distinct = {frozenset(p) for p in separate_pairs}
    if any(len(p) != 2 for p in distinct):
        raise ValueError('distinct version pairs required')
    articles, observations = {}, defaultdict(list)
    urls = defaultdict(set)
    for i, group in enumerate(groups):
        for j, article in enumerate(group.get('articles', [])):
            if not isinstance(article.get('url'), str) or not article['url']:
                raise ValueError('article URL required')
            vid = version_id(article)
            articles.setdefault(vid, copy.deepcopy(article))
            observations[vid].append({'group_index': i, 'article_index': j, 'article': copy.deepcopy(article)})
            urls[article['url']].add(vid)
    for vid, occurrences in observations.items():
        articles[vid] = copy.deepcopy(min((o['article'] for o in occurrences), key=dumps))
        ids = set(bindings.get(vid, []))
        for occurrence in occurrences:
            group = groups[occurrence['group_index']]
            if group.get('event_id'):
                ids.add(group['event_id'])
            ids.update(group.get('event_ids', []))
        bindings[vid] = sorted(ids)
    scope = {vid: classify(a) for vid, a in articles.items()}
    for vid, event_ids in bindings.items():
        if vid in scope and event_ids and scope[vid]['status'] == 'outside_candidate':
            scope[vid].update(status='hold', reason='existing_event_assignment_requires_review')
    for vid, override in overrides.items():
        if vid not in scope or override.get('status') not in {'inside', 'hold', 'outside_candidate'} or not override.get('record_id'):
            raise ValueError('version-bound operator record required')
        scope[vid].update(status=override['status'], operator_record=copy.deepcopy(override), safe_exclusion_confirmed=False)
    buckets = defaultdict(list)
    blocked = {}
    for vid, article in articles.items():
        # Conflicting source metadata at identical article identity is uncertain.
        sources = {_source(o['article']) for o in observations[vid]}
        day = jst_day(article.get('published'))
        if not article.get('title') or len(sources) != 1 or None in sources or day is None:
            blocked[vid] = 'missing_or_conflicting_strict_metadata'
        elif len(urls[article['url']]) > 1:
            blocked[vid] = 'same_url_multiple_versions'
        else:
            buckets[(article['title'], next(iter(sources)), day)].append(vid)
    aliases = {vid: vid for vid in articles}
    cohorts = []
    for fields, members in sorted(buckets.items()):
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda vid: (articles[vid]['url'], vid))
        cid = digest({'exact_title_source_jst_day': fields, 'versions': sorted(members)})
        conflict = any(_separate(a, b, bindings, distinct) for i, a in enumerate(members) for b in members[i+1:])
        active = cid not in disabled_cohorts and not conflict
        if active:
            for vid in members:
                aliases[vid] = members[0]
        cohorts.append({'cohort_id': cid, 'members': members, 'representative': members[0],
                        'strict_metadata': {'title': fields[0], 'rss_source': fields[1], 'published_jst_day': fields[2]},
                        'active': active, 'reason': 'known_different_events' if conflict else 'operator_disabled' if cid in disabled_cohorts else 'strict_metadata_policy',
                        'residual_risk': 'same_day_title_source_can_still_be_different_articles',
                        'body_identity_proved': False, 'event_identity_proved': False})
    # Existing groups are NOT merged/rewritten. Alias only identical AI work units
    # whose full non-article context and represented members are exactly equal.
    work, deferrals, group_aliases, seen = [], [], {}, {}
    for index, group in enumerate(groups):
        members = [version_id(a) for a in group.get('articles', [])]
        if not members:
            deferrals.append({'group_index': index, 'reason': 'empty_group'})
            continue
        if all(scope[vid]['status'] == 'outside_candidate' for vid in members):
            deferrals.append({'group_index': index, 'reason': 'outside_candidate_reversible'})
            continue
        retained = [vid for vid in members if scope[vid]['status'] != 'outside_candidate']
        represented = sorted(set(aliases[vid] for vid in retained))
        # A retained revision must not inherit an outside-candidate representative.
        represented = sorted(set(vid if scope[aliases[vid]]['status'] == 'outside_candidate' else aliases[vid] for vid in retained))
        context = {k: v for k, v in group.items() if k != 'articles'}
        # Legacy count fields express cardinality, not event/decision identity.
        context.pop('article_count', None)
        context.pop('source_article_count', None)
        sig = digest([context, represented, [scope[vid]['status'] for vid in represented]])
        if sig in seen:
            group_aliases[str(index)] = seen[sig]
        else:
            seen[sig] = index
            projected = copy.deepcopy(group)
            projected['articles'] = [copy.deepcopy(articles[vid]) for vid in represented]
            projected['article_count'] = len(represented)
            projected['source_article_count'] = len(represented)
            work.append({'original_group_index': index, 'projected_group': projected,
                         'represented_versions': represented, 'original_versions': members})
    return {'schema': 1, 'rule_id': rule_id(),
            'context_hash': digest([groups, {'separate_pairs': sorted(sorted(x) for x in distinct),
               'event_bindings': event_bindings or {}, 'disabled_cohorts': sorted(disabled_cohorts),
               'overrides': overrides}, rule_id()]), 'original_groups': groups,
            'articles': articles, 'occurrences': dict(observations), 'scope': scope,
            'aliases': aliases, 'cohorts': cohorts, 'representation_blocked': blocked,
            'work_groups': work, 'deferred_groups': deferrals, 'work_group_aliases': group_aliases,
            'source_groups_changed': False, 'can_publish': False, 'ai_calls': 0,
            'requires_original_evidence_validation': True, 'dispatch_enabled': False}


def is_current(receipt, groups, **options):
    """An old receipt is history, never a current gate after any context change."""
    current = plan(groups, **options)
    return receipt.get('context_hash') == current['context_hash']


@contextmanager
def transaction(db):
    db.execute('SAVEPOINT pre_ai_save')
    try:
        yield
    except BaseException:
        db.execute('ROLLBACK TO pre_ai_save')
        db.execute('RELEASE pre_ai_save')
        raise
    else:
        db.execute('RELEASE pre_ai_save')


def open_store(path=':memory:'):
    if path != ':memory:':
        resolved = Path(path).resolve()
        if resolved.is_relative_to(Path(__file__).resolve().parents[2]):
            raise ValueError('store must be outside repository')
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE IF NOT EXISTS pre_ai_receipts(operation TEXT PRIMARY KEY, input_hash TEXT NOT NULL, recorded_at TEXT NOT NULL, result TEXT NOT NULL)')
    return db


def save_plan(db, operation, groups, recorded_at, *, fail_save=False, **options):
    """Append-only receipt. Replay identical operation; never rewrite old history.

    Replan with a NEW operation ID when article/rule/operator inputs change.
    No setter for production status, event IDs, evidence, approvals or budget.
    """
    if not isinstance(operation, str) or not operation.strip():
        raise ValueError('operation required')
    if not isinstance(recorded_at, datetime) or recorded_at.tzinfo is None:
        raise ValueError('timezone required')
    # Stable, JSON-compatible options also make reversal and audit reproducible.
    ih = digest([groups, options, rule_id()])
    with transaction(db):
        old = db.execute('SELECT input_hash,result FROM pre_ai_receipts WHERE operation=?', (operation,)).fetchone()
        if old:
            if old[0] != ih:
                raise ValueError('operation inputs changed; replan with new operation')
            return json.loads(old[1])
        result = plan(groups, **options)
        db.execute('INSERT INTO pre_ai_receipts VALUES(?,?,?,?)', (operation, ih, recorded_at.isoformat(), dumps(result)))
        if fail_save:
            raise RuntimeError('simulated save failure')
        return result


def restore(receipt):
    """Explicit undo of projection; copies every original group and observation."""
    return copy.deepcopy(receipt['original_groups'])
