"""国家统计局原文：只提取有统计月份和实际发布日期的两条库存周期序列。"""
import calendar
import datetime as dt
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

from bs4 import BeautifulSoup

NBS = 'https://www.stats.gov.cn/sj/zxfb/'
AXES = ('new_orders', 'finished_inventory')


def article_kind(title):
    if '采购经理指数运行情况' in title and '解读' not in title:
        return AXES[0]
    if re.search(r'全国规模以上工业企业利润', title) and '解读' not in title:
        return AXES[1]


def parse_release(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title, published = (soup.find('meta', attrs={'name': key}) for key in ('ArticleTitle', 'PubDate'))
    if not title or not published:
        return None
    title = title.get('content', '')
    kind = article_kind(title)
    period = re.search(r'(20\d{2})年(?:1[—–－-])?(\d{1,2})月', title)
    if not kind or not period and not re.match(r'20\d{2}年全国规模以上工业企业利润', title):
        return None
    year, month = tuple(map(int, period.groups())) if period else (int(title[:4]), 12)
    date = dt.date(year, month, calendar.monthrange(year, month)[1])
    published = dt.date.fromisoformat(published.get('content', '')[:10].replace('/', '-'))
    # A PMI bulletin can be released before month-end. Do not replace its publication date with the period end.
    text = re.sub(r'\s+', '', (soup.select_one('.TRS_UEDITOR, .TRS_Editor') or soup).get_text('', strip=True))
    if kind == 'new_orders':
        text = text.partition('一、中国制造业采购经理指数运行情况')[2].partition('二、')[0]
        hit = re.search(r'新订单指数为(\d+(?:\.\d+)?)%', text)
        if not hit or not 0 <= (value := float(hit[1])) <= 100:
            return None
    else:
        hit = re.search(r'产成品存货(?:为)?[\d,.]+(?:万亿|亿)元[，,](?:同比)?(增长|下降)([\d.]+)%', text)
        if not hit:
            return None
        value = float(hit[2]) * (-1 if hit[1] == '下降' else 1)
    return kind, {'date': date.isoformat(), 'period': date.strftime('%Y-%m'), 'value': value,
                  'published_on': published.isoformat(), 'url': url}


def fetch_releases(fetch, end, old, pages=3):
    """Merge by source article; a missing page must not erase archived observations."""
    urls, failures = {}, 0
    for i in range(pages):
        try:
            soup = BeautifulSoup(fetch(urljoin(NBS, f'index_{i}.html') if i else NBS), 'html.parser')
        except Exception:
            failures += 1
            continue
        for a in soup.select('a[title][href]'):
            href = urljoin(NBS, a['href'])
            kind = article_kind(a['title'])
            if kind and href.startswith(NBS):
                urls[href] = kind
    archive = {k: {r['url']: r for r in old.get(k, []) if r.get('url') and r.get('published_on', '') <= end} for k in AXES}
    def read(url):
        try:
            return parse_release(fetch(url), url)
        except Exception:
            return None
    # Archived original releases are not overwritten with a later vintage of the same URL.
    pending = [u for u, k in urls.items() if u not in archive[k]]
    with ThreadPoolExecutor(max_workers=3) as pool:
        for item in pool.map(read, pending):
            if not item:
                failures += 1
                continue
            key, row = item
            if row['published_on'] <= end:
                archive[key][row['url']] = row
    return {k: sorted(rows.values(), key=lambda r: (r['date'], r['published_on'])) for k, rows in archive.items()}, failures
