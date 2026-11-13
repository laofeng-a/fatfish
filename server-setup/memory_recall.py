"""Small lexical recall and reading-topic metadata, without extra model calls."""
import collections
import datetime as dt
import heapq
import json
import math
import re
import time
import unicodedata
from urllib.parse import urlsplit

DAY = 86400
STOP = set('什么 之前 记得 我们 那个 一下 最近 你还 这个 那些 有没有 是不是 现在 以前 一个 的时候 帮我 查查 找找 哪个 怎么 哪里 怎样 为什么'.split())
# Denial only. "不要香菜" is still about 香菜; "不是宠物" is not evidence of a pet.
_DENIAL = re.compile(r'(?:并不是|不是|并非)([\u3400-\u9fff]{1,8})')
_TOKEN = re.compile(r'[a-z0-9_]+|[\u3400-\u9fff]+')
_BM25_K1 = 1.2
_BM25_B = 0.75


def normalized(text):
    return unicodedata.normalize('NFKC', str(text)).casefold()


def _pieces(word):
    if re.fullmatch(r'[a-z0-9_]+', word) or len(word) <= 2:
        return [word]
    return [word[i:i + n] for n in (2, 3) for i in range(len(word) - n + 1)]


def terms(query):
    result = []
    for word in _TOKEN.findall(normalized(query)):
        result.extend(piece for piece in _pieces(word) if piece not in STOP)
    return sorted(set(result))


def _denial_spans(text):
    return [(match.start(1), match.end(1)) for match in _DENIAL.finditer(text)]


def _overlaps(start, end, spans):
    return any(start < stop and end > begin for begin, stop in spans)


def document_terms(text):
    """Positive term counts. Terms inside 不是/并非 are not evidence."""
    content = normalized(text)
    spans = _denial_spans(content)
    counts = collections.Counter()
    for match in _TOKEN.finditer(content):
        word, base = match.group(), match.start()
        for offset, piece in _located_pieces(word):
            if piece in STOP:
                continue
            start = base + offset
            if _overlaps(start, start + len(piece), spans):
                continue
            counts[piece] += 1
    return counts, max(len(content), 1)


def _located_pieces(word):
    if re.fullmatch(r'[a-z0-9_]+', word) or len(word) <= 2:
        yield 0, word
        return
    for size in (2, 3):
        for index in range(len(word) - size + 1):
            yield index, word[index:index + size]


def prepare(query, texts):
    """IDF over the candidate set, ignoring denial spans and absent query pieces."""
    documents = [document_terms(text) for text in texts]
    total = len(documents) or 1
    keys = terms(query)
    counts = collections.Counter()
    for frequencies, _length in documents:
        counts.update(key for key in keys if frequencies[key])
    idf = {key: math.log(1 + (total - counts[key] + 0.5) / (counts[key] + 0.5)) for key in keys if counts[key]}
    average = sum(length for _counts, length in documents) / total
    return idf, average or 1


def weights(query, texts):
    """Rare matching terms outweigh chatter. Same IDF map prepare() uses."""
    return prepare(query, texts)[0]


def _middle_only(query, frequencies):
    """True when the match covers neither end of a longer query.

    "另一个群里的兰州牛肉面订单" overlaps a private noodle note only in the middle.
    "那家面馆" and "充电头收在哪个抽屉" still touch one end or both.
    """
    if not query:
        return False
    mask = [False] * len(query)
    for match in _TOKEN.finditer(query):
        word, base = match.group(), match.start()
        for offset, piece in _located_pieces(word):
            if piece in STOP or not frequencies[piece]:
                continue
            for index in range(offset, offset + len(piece)):
                mask[base + index] = True
    if not any(mask):
        return False
    lead = 0
    while lead < len(mask) and not mask[lead]:
        lead += 1
    trail = 0
    while trail < len(mask) and not mask[-1 - trail]:
        trail += 1
    return lead >= 2 and trail >= 2


def rank_match(text, query, idf, average_length):
    """BM25 over CJK bigrams/trigrams and ASCII words, plus exact-phrase containment.

    Full-query containment is reported separately so an exact quote outranks a
    shorter partial overlap. Recency stays a caller-side tiebreak.
    """
    content = normalized(text)
    folded = normalized(query).strip()
    exact = bool(folded) and folded in content
    frequencies, length = document_terms(content)
    if not exact and _middle_only(folded, frequencies):
        return False, 0.0, []
    hits = []
    score = 0.0
    for key, weight in idf.items():
        freq = frequencies[key]
        if not freq:
            continue
        hits.append(key)
        denominator = freq + _BM25_K1 * (1 - _BM25_B + _BM25_B * length / average_length)
        score += weight * (freq * (_BM25_K1 + 1)) / denominator
    if not hits and not exact:
        return False, 0.0, []
    return exact, score, hits


def relevance(text, query, term_weights):
    _exact, score, hits = rank_match(text, query, term_weights, max(len(normalized(text)), 1))
    if not hits and not _exact:
        return 0.0, []
    if _exact:
        score += 0.35
    return score, hits


def freshness(row, now):
    if row.get('category') == 'core':
        return 1.0
    importance = row.get('importance', 0.5)
    half_life = 30 * (1 + 2 * importance)
    age = max(0, now - (row.get('activation_at') or row['created'])) / DAY
    return row.get('activation', 1.0) * 2 ** (-age / half_life)


def link_keys(text):
    text = str(text)
    keys = set(re.findall(r'BV[0-9A-Za-z]{10}', text))
    for url in re.findall(r'https?://[^\s<>"\]\}]+', text):
        url = url.rstrip(".,，。！？!?)）'\\")
        parsed = urlsplit(url)
        host = parsed.netloc.casefold()
        match = re.search(r'/status/(\d+)', parsed.path)
        if host in ('x.com', 'www.x.com', 'twitter.com', 'www.twitter.com') and match:
            keys.add('x:' + match[1])
        else:
            keys.add(host + parsed.path.rstrip('/'))
    return keys


def source_url(text):
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        payload = None
    found = []
    def find(value):
        if isinstance(value, dict):
            for key in ('url', 'webpage_url', 'video_url', 'link'):
                item = value.get(key)
                if isinstance(item, str) and item.startswith(('https://', 'http://')):
                    found.append(item)
            bvid = value.get('bvid')
            if isinstance(bvid, str) and re.fullmatch(r'BV[0-9A-Za-z]{10}', bvid):
                found.append('https://www.bilibili.com/video/' + bvid)
            for item in value.values():
                find(item)
        elif isinstance(value, list):
            for item in value:
                find(item)
    find(payload)
    if found:
        unique = {}
        for url in found:
            bvid = re.search(r'BV[0-9A-Za-z]{10}', url)
            x = re.search(r'(?:x|twitter)\.com/[^/]+/status/(\d+)', url)
            key = bvid[0] if bvid else ('x:' + x[1] if x else url.split('?')[0])
            unique.setdefault(key, url)
        return next(iter(unique.values())) if len(unique) == 1 else ''
    match = re.search(r'https?://[^\s<>"\]\}]+', str(text))
    return match[0].rstrip(".,，。！？!?)）'\\") if match else ''


def search_notes(notes, query, limit=12):
    documents = [(path, notes.content(path)) for path in notes.files()]
    idf, average = prepare(query, (text for _, text in documents))
    matches = []
    for order, (path, text) in enumerate(documents):
        exact, score, hits = rank_match(text, query, idf, average)
        if not score and not exact:
            continue
        # Exact quotes stay ahead of a shorter partial overlap. File order is the tiebreak.
        rank = score + (10 if exact else 0)
        folded = normalized(text)
        position = min((folded.find(term) for term in hits), default=0)
        start = max(0, position - 160)
        matches.append((rank, -order, {'path': notes.name(path), 'snippet': text[start:start+1200],
                                      'revision': notes.revision(text), 'score': round(rank, 4)}))
    return [item[2] for item in heapq.nlargest(limit, matches, key=lambda item: item[:2])]


class MemoryRecall:
    def __init__(self, runtime):
        self.r = runtime
        with runtime.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS memory_salience(
                memory_id INTEGER PRIMARY KEY,importance REAL NOT NULL DEFAULT 0.5,
                activation REAL NOT NULL DEFAULT 1,activation_at REAL,
                reinforced_source_id INTEGER,reinforce_count INTEGER NOT NULL DEFAULT 0,
                topic_state TEXT NOT NULL DEFAULT 'queued',shared_at REAL)''')

    @staticmethod
    def options(args):
        importance = args.get('importance', 0.5)
        if type(importance) not in (int, float) or not math.isfinite(importance) or not 0 <= importance <= 1:
            raise ValueError('importance填写0至1')
        state = args.get('topic_state', 'queued')
        if state not in ('queued', 'shared', 'closed'):
            raise ValueError('topic_state填写queued/shared/closed')
        return float(importance), state

    def saved(self, db, identifier, args, source_id, duplicate=False):
        importance, state = self.options(args)
        old = db.execute('SELECT * FROM memory_salience WHERE memory_id=?', (identifier,)).fetchone()
        now = time.time()
        if old:
            data = dict(old)
            data.update(category=args['category'], created=now)
            activation = freshness(data, now)
            activation = activation + 0.2 * (1 - activation) if duplicate else 1.0
            importance = importance if 'importance' in args else old['importance']
            state = state if 'topic_state' in args else old['topic_state']
        else:
            activation = 1.0
        shared = now if state == 'shared' else (old['shared_at'] if old else None)
        db.execute('''INSERT INTO memory_salience(memory_id,importance,activation,activation_at,
                   reinforced_source_id,reinforce_count,topic_state,shared_at) VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(memory_id) DO UPDATE SET importance=excluded.importance,
                   activation=excluded.activation,activation_at=excluded.activation_at,
                   reinforced_source_id=excluded.reinforced_source_id,
                   reinforce_count=memory_salience.reinforce_count+?,
                   topic_state=excluded.topic_state,shared_at=excluded.shared_at''',
                   (identifier, importance, activation, now, source_id, int(duplicate), state, shared, int(duplicate)))

    @staticmethod
    def _where(chat, args, now):
        category = args.get('category', 'all')
        if category not in ('all', 'core', 'interaction', 'inference', 'opinion', 'topic'):
            raise ValueError('未知的记忆类别')
        state = args.get('topic_state', 'all')
        if state not in ('all', 'queued', 'shared', 'closed'):
            raise ValueError('未知的话题状态')
        clause = 'm.chat=? AND m.active=1 AND (m.expires IS NULL OR m.expires>?)'
        params = [chat, now]
        if category != 'all':
            clause += ' AND m.category=?'; params.append(category)
        if state != 'all':
            clause += " AND m.category='topic' AND COALESCE(s.topic_state,'queued')=?"; params.append(state)
        return clause, params

    def find(self, args, chat, db=None):
        if db is None:
            with self.r.db() as connection:
                return self.find(args, chat, connection)
        query = args.get('query', '')
        if not isinstance(query, str):
            raise ValueError('query需要文字')
        query = query[:120].strip()
        limit = args.get('limit', 12)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('limit填写1至50')
        now = time.time()
        clause, params = self._where(chat, args, now)
        sql = '''SELECT m.*,COALESCE(s.importance,0.5) AS importance,
                 COALESCE(s.activation,1) AS activation,s.activation_at,
                 COALESCE(s.topic_state,'queued') AS topic_state,s.shared_at
                 FROM memories m LEFT JOIN memory_salience s ON s.memory_id=m.id WHERE ''' + clause
        rows = [dict(row) for row in db.execute(sql, params)]
        idf, average = prepare(query, (row['content'] for row in rows)) if query else ({}, 1)
        dated = bool(re.search(r'\d{4}[年/-]|\d{1,2}月', query))
        def ranked():
            for row in rows:
                if query:
                    exact, rel, hits = rank_match(row['content'], query, idf, average)
                    if not rel and not exact:
                        continue
                else:
                    exact, rel, hits = False, 1.0, []
                fresh = freshness(row, now)
                score = rel * (0.8 + 0.15 * (1 if dated else fresh) + 0.05 * row['importance'])
                if exact:
                    score += 10
                row.update(score=round(score, 5), match_terms=hits,
                           freshness=round(fresh, 5), half_life_days=None if row['category']=='core' else 30*(1+2*row['importance']))
                yield score, row['created'], row['id'], row
        selected = [item[3] for item in heapq.nlargest(limit, ranked(), key=lambda item: item[:3])]
        for row in selected:
            source = db.execute('SELECT content FROM events WHERE id=? AND chat=?', (row['source_id'], chat)).fetchone()
            row['source_url'] = source_url(row['content']) or source_url(row['quote']) or (source_url(source['content']) if source else '')
        return selected

    def mark_shared(self, db, chat, confirmed):
        keys = link_keys(confirmed)
        if not keys:
            return 0
        rows = db.execute('''SELECT m.id,m.content,e.content AS source_content
             FROM memories m LEFT JOIN memory_salience s ON m.id=s.memory_id
             LEFT JOIN events e ON e.id=m.source_id AND e.chat=m.chat
             WHERE m.chat=? AND m.category='topic' AND m.active=1
             AND COALESCE(s.topic_state,'queued')='queued' ''', (chat,)).fetchall()
        count = 0
        for row in rows:
            # Match the exact curated excerpt/link, not every URL in a search response.
            linked = link_keys(row['content'])
            if not linked:
                source = db.execute('SELECT quote FROM memories WHERE id=?', (row['id'],)).fetchone()
                linked = link_keys(source['quote'])
            if not linked:
                url = source_url(row['source_content'] or '')
                linked = link_keys(url)
            if keys.intersection(linked):
                db.execute('''INSERT INTO memory_salience(memory_id,topic_state,shared_at)
                           VALUES(?,'shared',?) ON CONFLICT(memory_id) DO UPDATE
                           SET topic_state='shared',shared_at=excluded.shared_at''', (row['id'], time.time()))
                count += 1
        return count

    def background_context(self, db, chat):
        topics = self.find({'category': 'topic', 'topic_state': 'queued', 'limit': 3}, chat, db)
        echoes = self.find({'category': 'interaction', 'limit': 2}, chat, db)
        if not topics and not echoes:
            return ''
        compact = lambda row: {key: row[key] for key in ('id','category','content','source_id','source_url','created')}
        return ('[可聊素材与交流片段]\n以前真实留下的内容，供你挑选、继续了解或自然翻篇。'
                '按眼下兴趣接话，用自己的话说，不必逐条汇报。\n'
                + json.dumps({'topics': [compact(r) for r in topics], 'interactions': [compact(r) for r in echoes]}, ensure_ascii=False))
