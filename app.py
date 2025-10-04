# app.py
# -*- coding: utf-8 -*-
"""
Flask UI for the global faculty scraper (JS rendering is ALWAYS ON).
Run:
  pip install flask requests beautifulsoup4 lxml pandas openpyxl tldextract playwright
  playwright install chromium
  # (Linux/WSL) sudo playwright install-deps
Start:
  python app.py
Open:
  http://127.0.0.1:5000
"""

from __future__ import annotations

import io
import re
import os
import time
import uuid
import json
import random
import logging
import threading
from typing import Dict, List, Optional, Tuple, Set
from urllib.parse import urljoin, urlparse

from flask import Flask, request, jsonify, send_file, render_template_string

import pandas as pd
import requests
import tldextract
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter, Retry
from urllib import robotparser

# ----------------------------- Logging ---------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

# ----------------------------- Scraper config --------------------------------
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
REQUEST_TIMEOUT = (10, 30)
MAX_PAGINATION_PAGES = 20

# Discovery filters
NON_PERSON_KEYWORDS = {
    "login","signin","signup","admin","rss","download","map","index","search","sitemap",
    "news","notice","event","seminar","project","publication","publications","file","files","press",
    "undergrad","graduate","phd","masters","postdoc","students","jobs","careers","alumni","giving",
    "policy","privacy","cookie","terms","media","branding","about","contact",
    "招生","学生","本科","硕士","博士","下载","新闻","通知","公告","招聘","隐私","政策","关于","联系我们",
}
NEXT_TEXTS = {"下一页","下页","下一頁","Next","next","›","»",">","后一页","下一个","More","Older"}

# Role hints for *teaching staff only*
ROLE_WORDS = {
    # EN
    "professor","associate professor","assistant professor","lecturer","senior lecturer",
    "reader","chair","chairperson","faculty","teaching","instructor",
    # CN
    "教授","副教授","助理教授","讲师","导师","教师","教员",
}
URL_ROLE_HINTS = [
    "people","faculty","staff","person","profile","member","team","group","directory",
    "teacher","teachers","faculty_profile"
]

# Email handling
ACADEMIC_EMAIL_PREF = re.compile(r"\.(edu(\.[a-z]{2})?|ac(\.[a-z]{2})?|edu\.[a-z]{2,}|ac\.[a-z]{2,}|cn|org)$", re.I)
TRAILING_PUNCT = re.compile(r"[，,；;。、\s]+$")
BASIC_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,24}", re.I)

GENERIC_EMAIL_USERS = {
    "info","contact","office","admissions","apply","enquiry","enquiries","inquiries","inquiry",
    "press","media","pr","marketing","outreach","external","international","webmaster","noreply",
    "no-reply","admin","administrator","root","support","help","service","services","career","careers",
    "hr","jobs","recruit","recruitment","news","events","alumni","oic","promote","postmaster","security","abuse",
    "secretary","office-of","frontdesk","student","students","library","postgrad","undergrad"
}
PERSONAL_FREE_DOMAINS = {
    "gmail.com","outlook.com","hotmail.com","yahoo.com","yahoo.co.uk","proton.me","icloud.com","qq.com","163.com","126.com","yeah.net"
}

# Phone regexes
PHONE_REGEXES = [
    re.compile(r"(\+\d{1,3}[\s-]?\(?\d{1,4}\)?(?:[\s-]?\d{2,4}){2,4})"),
    re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)"),
    re.compile(r"(0\d{2,3}-\d{7,8})"),
]

EMAIL_TOKEN_PATTERNS = [
    (re.compile(r"\s*\[?\s*at\s*\]?\s*", re.I), "@"),
    (re.compile(r"\s*\(?\s*at\s*\)?\s*", re.I), "@"),
    (re.compile(r"\s*＠\s*"), "@"),
    (re.compile(r"\s*\[?\s*#\s*\]?\s*", re.I), "@"),
    (re.compile(r"\s*\(\s*dot\s*\)|\[\s*dot\s*\]|\{\s*dot\s*\}", re.I), "."),
    (re.compile(r"[（\)]?点[）\)]?", re.I), "."),
    (re.compile(r"\s+dot\s+", re.I), "."),
    (re.compile(r"\s+at\s+", re.I), "@"),
    (re.compile(r"\s*\(remove\)|\(no\s*spam\)", re.I), ""),
]

RESEARCH_HEADERS = [
    "Research Interests","Research Interest","Research Area","Research Areas",
    "Areas of Research","Fields of Research","Interests","Areas of Interest",
    "研究方向","研究领域","研究兴趣","主要研究方向","学术方向",
]
NAME_HINT_SELECTORS = ["h1","h2",".name",".person-name",".faculty-name",".prof-name",".teacherName","title"]
SCHOOL_HINT_KEYWORDS = [
    "School","Department","College","Faculty","Institute","Center","Centre","Laboratory","Lab",
    "学院","系","部门","部","研究所","实验室","中心",
]
LISTING_CONTAINERS = [
    ".faculty",".people",".person",".person-list",".profile-list",".team",".members",".directory",
    ".teacher",".teacher-list",".staff",".list",".grid",".grid-list",".cards",".card-list",
    ".news_list",".content_list",".listright",".box",".row",".col",".grid-container",".collection",
    "ul","table","article","section",
]

# ----------------------------- Data / helpers --------------------------------
class TranslatorWrapper:
    def __init__(self):
        self.cache: Dict[tuple, str] = {}
        self.mode = None
        try:
            from googletrans import Translator  # type: ignore
            self._gt = Translator()
            self.mode = "googletrans"
        except Exception:
            try:
                from deep_translator import GoogleTranslator  # type: ignore
                self._deep = GoogleTranslator(source="auto", target="en")
                self.mode = "deep"
            except Exception:
                logging.info("Translator not available; install googletrans or deep-translator.")
                self.mode = None

    def translate(self, text: str, target="en") -> str:
        if not text or self.mode is None:
            return text
        key = (text, "auto", target)
        if key in self.cache:
            return self.cache[key]
        try:
            out = self._gt.translate(text, dest=target).text if self.mode == "googletrans" else self._deep.translate(text)
            self.cache[key] = out
            return out
        except Exception:
            return text
class HttpClient:
    """Playwright JS rendering is ALWAYS enabled in this app."""
    def __init__(self, delay_min: float, delay_max: float):
        self.session = requests.Session()
        retries = Retry(
            total=5,
            backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD"]),
            raise_on_status=False
        )
        adapter = HTTPAdapter(max_retries=retries, pool_connections=16, pool_maxsize=32)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update(HEADERS)

        self.delay_min = delay_min
        self.delay_max = delay_max
        self._robots: Dict[str, robotparser.RobotFileParser] = {}

        # JS state/diagnostics
        self._js = None            # playwright enabled? True/False/None (unknown)
        self.js_error = ""         # human-readable reason if JS disabled

        self.init_js()             # force-on at construction

    def _sleep(self):
        time.sleep(random.uniform(self.delay_min, self.delay_max))

    def allowed_by_robots(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
            base = f"{parsed.scheme}://{parsed.netloc}"
            if base not in self._robots:
                rp = robotparser.RobotFileParser()
                rp.set_url(urljoin(base, "/robots.txt"))
                try:
                    rp.read()
                except Exception as e:
                    logging.warning("robots.txt fetch failed for %s: %s", base, e)
                self._robots[base] = rp
            return self._robots[base].can_fetch(USER_AGENT, url)
        except Exception:
            return True

    def fetch(self, url: str) -> Optional[str]:
        """Try normal GET, then JS fallback (always available)."""
        if not self.allowed_by_robots(url):
            logging.warning("robots disallow: %s", url)
            return None
        try:
            self._sleep()
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
            if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "latin-1"):
                resp.encoding = resp.apparent_encoding or "utf-8"
            if resp.status_code == 200 and resp.text:
                return resp.text
        except Exception as e:
            logging.info("fetch error: %s (%s)", url, e)
        # JS fallback
        return self.fetch_js(url)

    def init_js(self):
        # If we've already tried to init JS once, don't try again.
        if self._js is not None:
            return

        # 1) Is Playwright even installed?
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except Exception:
            logging.error("Playwright not installed. Run: pip install playwright && playwright install chromium")
            self._js = False
            self.js_error = "Playwright not installed"
            return

        # 2) Try to launch Chromium and create a context
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._context = self._browser.new_context(user_agent=USER_AGENT, locale="en-US")
            self._js = True
            self.js_error = ""
        except Exception as e:
            logging.error("Playwright init failed: %s", e)
            self._js = False
            self.js_error = f"Playwright init failed: {e}"

    def fetch_js(self, url: str) -> Optional[str]:
        if not self._js:
            return None
        try:
            page = self._context.new_page()
            page.set_default_navigation_timeout(45000)
            page.set_default_timeout(45000)
            page.goto(url, wait_until="networkidle")
            time.sleep(0.5)
            html = page.content()
            page.close()
            return html
        except Exception as e:
            logging.info("JS fetch failed: %s (%s)", url, e)
            return None

    def close(self):
        try:
            if getattr(self, "_context", None):
                self._context.close()
            if getattr(self, "_browser", None):
                self._browser.close()
            if getattr(self, "_pw", None):
                self._pw.stop()
        except Exception:
            pass

def init_js(self):
    if self._js is not None:
        return
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:
        logging.error("Playwright not installed. Run: pip install playwright && playwright install chromium")
        self._js = False
        self.js_error = "Playwright not installed"
        return
    try:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._context = self._browser.new_context(user_agent=USER_AGENT, locale="en-US")
        self._js = True
    except Exception as e:
        logging.error("Playwright init failed: %s", e)
        self._js = False
        self.js_error = f"Playwright init failed: {e}"


    def fetch_js(self, url: str) -> Optional[str]:
        if not self._js:
            return None
        try:
            page = self._context.new_page()
            page.set_default_navigation_timeout(45000)
            page.set_default_timeout(45000)
            page.goto(url, wait_until="networkidle")
            time.sleep(0.5)
            html = page.content()
            page.close()
            return html
        except Exception as e:
            logging.info("JS fetch failed: %s (%s)", url, e)
            return None

    def close(self):
        try:
            if getattr(self, "_context", None):
                self._context.close()
            if getattr(self, "_browser", None):
                self._browser.close()
            if getattr(self, "_pw", None):
                self._pw.stop()
        except Exception:
            pass

# ----------------------------- Utils -----------------------------------------
TRAIL_CLEAN = re.compile(r"[，,；;。、\s]+$")

def strip_trailing_punct(s: str) -> str:
    return TRAIL_CLEAN.sub("", s.strip()) if s else s

def text_of(el) -> str:
    t = el.get_text(" ", strip=True)
    t = t.replace("ï¼š","：")
    return strip_trailing_punct(re.sub(r"\s+", " ", t))

def tidy_title_name(t: str) -> str:
    t = re.sub(r"\s+", " ", t.strip())
    t = re.split(r"\s+\|\s+|\s+—\s+|\s+-\s+", t)[0].strip()
    t = re.sub(r"[-–—]\s*Department.*$", "", t, flags=re.I)
    t = re.sub(r",?\s*University.*$", "", t, flags=re.I)
    if re.search(r"[\u4e00-\u9fff]", t):
        t = re.split(r"[，,。、：:(（)]", t)[0]
    return t if 1 <= len(t) <= 80 else ""

def infer_university_from_url(url: str) -> str:
    try:
        ext = tldextract.extract(url)
        name = ext.domain.replace("-", " ").title()
        if all(x not in name for x in ("University","College","学院","大学")):
            name = f"{name} University"
        return name
    except Exception:
        return ""

def registered_domain(url: str) -> str:
    try:
        ext = tldextract.extract(url)
        return ext.registered_domain or ""
    except Exception:
        return ""

def looks_like_human_name(name: str) -> bool:
    if not name:
        return False
    if re.search(r"(About|Home|Team|List|Search|Hot|Institutions|Laboratories|Page)", name, re.I):
        return False
    if re.search(r"@|\.com|\.edu|\.cn", name, re.I):
        return False
    if re.search(r"[\u4e00-\u9fff]", name):
        return True
    tokens = re.findall(r"[A-Za-z]+", name)
    return len(tokens) >= 2 and len("".join(tokens)) >= 3

def page_has_teaching_role(txt: str) -> bool:
    low = txt.lower()
    if any(h in low for h in URL_ROLE_HINTS):
        return True
    for w in ROLE_WORDS:
        if w.isascii():
            if re.search(rf"\b{re.escape(w)}\b", low):
                return True
        else:
            if w in low:
                return True
    return False

# ----------------------------- Email extraction -------------------------------
def normalize_email(text: str) -> Optional[str]:
    if not text:
        return None
    s = text
    for pat, repl in EMAIL_TOKEN_PATTERNS:
        s = pat.sub(repl, s)
    s = s.replace("（","(").replace("）",")")
    s = s.replace("[at]","@").replace("{at}","@").replace("[dot]",".").replace("{dot}",".")
    m = BASIC_EMAIL_RE.search(s)
    if m:
        return m.group(0)
    m2 = re.search(r"([A-Za-z0-9._%+-]+)\s+([A-Za-z0-9.-]+\.[A-Za-z]{2,24})", s)
    if m2:
        cand = f"{m2.group(1)}@{m2.group(2)}"
        if BASIC_EMAIL_RE.fullmatch(cand): return cand
    m3 = re.search(r"([A-Za-z0-9._%+-]+)\s+(([A-Za-z0-9-]+)\s+(edu|ac|com|org|cn|uk|de|fr|jp|kr)(\s+\w{2})?)", s, re.I)
    if m3:
        parts = re.findall(r"[A-Za-z0-9-]+", m3.group(2))
        domain = ".".join(parts[-3:]) if len(parts) >= 2 else m3.group(2).replace(" ", ".")
        cand = f"{m3.group(1)}@{domain}"
        if BASIC_EMAIL_RE.fullmatch(cand): return cand
    return None

def extract_emails(text: str) -> List[str]:
    if not text: return []
    candidates: List[str] = []
    for m in BASIC_EMAIL_RE.finditer(text):
        candidates.append(m.group(0))
    tokens = re.split(r"[\s,;|/<>]+", text)
    for tok in tokens:
        e = normalize_email(tok)
        if e: candidates.append(e)
    words = re.split(r"(\s+)", text)
    for i in range(max(0, len(words)-4)):
        e = normalize_email("".join(words[i:i+5]))
        if e: candidates.append(e)
    # uniq
    seen=set(); uniq=[]
    for e in candidates:
        if e not in seen:
            seen.add(e); uniq.append(e)
    return uniq

def name_affinity(name: str, local_part: str) -> int:
    tokens = [t.lower() for t in re.findall(r"[A-Za-z]+", name)]
    local = local_part.lower()
    score = 0
    for t in tokens:
        if len(t) >= 2 and (t in local or local in t):
            score += 1
    return score

def is_academic_domain(domain: str) -> bool:
    return bool(ACADEMIC_EMAIL_PREF.search("." + domain.lower()))

def email_is_generic(local: str) -> bool:
    l = local.lower()
    return l in GENERIC_EMAIL_USERS or l.startswith(("no-reply","noreply","postmaster"))

def email_allowed(email: str, site_regdom: str) -> bool:
    """Keep only teaching-looking institutional emails:
       - reject generic users (info, alumni, etc.)
       - prefer same registered domain; otherwise require academic TLD
       - reject common personal/free-mail providers
    """
    if "@" not in email:
        return False
    local, domain = email.split("@", 1)
    dl = domain.lower(); ll = local.lower()

    if email_is_generic(ll):
        return False
    if dl in PERSONAL_FREE_DOMAINS:
        return False

    same_regdom = bool(site_regdom and (dl == site_regdom or dl.endswith("." + site_regdom)))
    if same_regdom:
        return True

    # otherwise, must be academic
    return is_academic_domain(dl)

def pick_best_email(name: str, emails: List[str], site_regdom: str) -> str:
    best = ""
    best_key = None
    for e in emails:
        if "@" not in e:
            continue
        local, domain = e.split("@", 1)
        if not email_allowed(e, site_regdom):
            continue
        same_domain = int(domain.lower() == site_regdom or domain.lower().endswith("." + site_regdom)) if site_regdom else 0
        academic = int(is_academic_domain(domain))
        aff = name_affinity(name, local)
        length_penalty = min(len(local), 20)
        key = (-same_domain, -academic, -aff, length_penalty)  # lower is better
        if best_key is None or key < best_key:
            best_key, best = key, e
    return best

def extract_phones(text: str) -> List[str]:
    if not text: return []
    out=[]
    for rx in PHONE_REGEXES:
        for m in rx.finditer(text):
            ph = m.group(0)
            if len(re.sub(r"\D","",ph))>=7 and ph not in out:
                out.append(ph)
    return out

# ----------------------------- Discovery -------------------------------------
def is_person_link(a_tag, base_url: str) -> bool:
    href = a_tag.get("href") or ""
    if not href or href.startswith("#"): return False
    href_abs = urljoin(base_url, href)
    path = urlparse(href_abs).path.lower()
    if any(k in path for k in NON_PERSON_KEYWORDS): return False
    text = text_of(a_tag)
    if not text or len(text)<2: return False
    if not re.search(r"[\u4e00-\u9fffA-Za-z]", text): return False
    if re.search(r"\b(More|Details|Profile|View|查看|详情)\b", text, re.I):
        parent = a_tag.find_parent(["li","div","article","tr"])
        if not parent or not parent.find(["h3","h4",".name",".person-name",".faculty-name"]):
            return False
    return True

def discover_profiles(listing_html: str, base_url: str) -> List[Dict[str,str]]:
    soup = BeautifulSoup(listing_html, "lxml")
    profiles=[]; seen=set()

    containers=[]
    for sel in LISTING_CONTAINERS:
        containers.extend(soup.select(sel))
    if not containers: containers=[soup]

    for cont in containers:
        for a in cont.select("a"):
            if not is_person_link(a, base_url): continue
            href_abs = urljoin(base_url, a.get("href"))
            if href_abs in seen: continue

            # name near card
            name_text=""
            parent = a.find_parent(["li","div","article","tr"])
            if parent:
                for sel in ("h3","h4",".name",".person-name",".faculty-name",".teacherName","td","th","strong","b"):
                    el = parent.select_one(sel)
                    if el: name_text=text_of(el); break
            if not name_text: name_text=text_of(a)
            name_text=tidy_title_name(name_text)
            if not looks_like_human_name(name_text): continue

            profiles.append({"name": name_text, "profile_url": href_abs})
            seen.add(href_abs)
    return profiles

def find_next_page(listing_html: str, base_url: str) -> Optional[str]:
    soup = BeautifulSoup(listing_html, "lxml")
    a = soup.select_one("a[rel=next]")
    if a and a.get("href"): return urljoin(base_url, a.get("href"))
    for link in soup.find_all("a"):
        txt = text_of(link)
        if txt in NEXT_TEXTS or re.fullmatch(r"(下一页|下页|下一頁|Next|››|>>|More)", txt):
            href = link.get("href")
            if href: return urljoin(base_url, href)
    return None

# ----------------------------- Profile parsing --------------------------------
def parse_label_value_blocks(soup: BeautifulSoup) -> Dict[str,str]:
    data={}
    for dl in soup.find_all("dl"):
        dts=dl.find_all("dt"); dds=dl.find_all("dd")
        if len(dts)==len(dds) and dts:
            for dt,dd in zip(dts,dds):
                k=text_of(dt); v=text_of(dd)
                if k and v: data[k]=v
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            tds=tr.find_all(["td","th"])
            if len(tds)==2:
                k=text_of(tds[0]); v=text_of(tds[1])
                if k and v: data[k]=v
    return data

def extract_name(soup: BeautifulSoup) -> str:
    for sel in NAME_HINT_SELECTORS:
        el = soup.select_one(sel)
        if el:
            t=tidy_title_name(text_of(el))
            if 1<=len(t)<=80 and looks_like_human_name(t):
                return t
    blocks = parse_label_value_blocks(soup)
    for k,v in blocks.items():
        if any(h in k.lower() for h in ("name","姓名")):
            t=tidy_title_name(v)
            if looks_like_human_name(t):
                return t
    return ""

def extract_school(soup: BeautifulSoup) -> str:
    for sel in (".breadcrumb",".breadcrumbs",".sitepath",".position",".location",".nav",".page-title",".path",".current-position"):
        for c in soup.select(sel):
            t=text_of(c)
            if t:
                parts = [p.strip() for p in re.split(r"\s*[>›/»]\s*", t) if p.strip()]
                if parts:
                    return parts[-1][:120]
                return t[:120]
    blocks = parse_label_value_blocks(soup)
    for k,v in blocks.items():
        if any(kw.lower() in k.lower() for kw in SCHOOL_HINT_KEYWORDS): return v[:120]
    for sel in ("h3","h4",".subtitle",".subheading"):
        el = soup.select_one(sel)
        if el:
            t=text_of(el)
            if any(kw.lower() in t.lower() for kw in SCHOOL_HINT_KEYWORDS): return t[:120]
    return ""

def extract_research_area(soup: BeautifulSoup) -> str:
    for header in soup.find_all(["h2","h3","strong","b"]):
        ht=text_of(header)
        if any(h.lower() in ht.lower() for h in RESEARCH_HEADERS):
            buf=[]
            for sib in header.next_siblings:
                if getattr(sib,"name",None) in ["h2","h3","strong","b"]: break
                if getattr(sib,"get_text",None):
                    s=text_of(sib).strip()
                    if s: buf.append(s)
                if len(" ".join(buf))>500: break
            if buf: return " ".join(buf)[:500]
    blocks = parse_label_value_blocks(soup)
    for k,v in blocks.items():
        if any(h.lower() in k.lower() for h in RESEARCH_HEADERS): return v[:500]
    body=text_of(soup)
    m=re.search(r"(Research (Interests?|Areas?)|研究(方向|领域|兴趣))[:：]\s*(.+)", body, re.I)
    return (strip_trailing_punct(m.group(4))[:500] if m else "")

def get_main_text(soup: BeautifulSoup) -> str:
    for sel in ("main","article","#content",".content",".page-content",".content-wrapper"):
        el = soup.select_one(sel)
        if el:
            return text_of(el)
    body = soup.body or soup
    for tag in body.select("header,nav,footer,aside,.footer,.site-footer,.global-footer,.topbar,.navbar"):
        tag.decompose()
    return text_of(body)

def parse_profile(profile_html: str, base_url: str, site_regdom: str) -> Tuple[str,str,str,str,str]:
    soup=BeautifulSoup(profile_html,"lxml")
    name=strip_trailing_punct(extract_name(soup))
    if not looks_like_human_name(name):
        return "", "", "", "", ""
    # keep only pages that look like *teaching staff*
    if not page_has_teaching_role(soup.get_text(" ", strip=True)):
        # allow if email clearly same-domain academic later
        pass

    school=strip_trailing_punct(extract_school(soup))
    research=strip_trailing_punct(extract_research_area(soup))

    texts=[]
    for a in soup.select("a[href^=mailto]"):
        texts.append(a.get("href") or ""); texts.append(text_of(a))
    texts.append(get_main_text(soup))
    full="\n".join(t for t in texts if t)

    emails_raw=extract_emails(full)
    email=pick_best_email(name, emails_raw, site_regdom)

    # require a valid staff email
    if not email:
        return "", "", "", "", ""

    phones=extract_phones(full)
    phone=phones[0] if phones else ""
    return name, school, research, email, phone

# ----------------------------- Orchestration ---------------------------------
def process_site(client: HttpClient, site_url: str, max_profiles: Optional[int], progress: Dict):
    results=[]
    visited=set()
    pages=0; next_url=site_url
    site_regdom = registered_domain(site_url)

    # Tell the UI if JS is off
    if client._js is False:
        progress["message"] = f"JS rendering disabled ({client.js_error}). Falling back to raw HTML."

    while next_url and pages<MAX_PAGINATION_PAGES:
        # robots check up-front so we can show a message
        if not client.allowed_by_robots(next_url):
            progress["message"] = f"Blocked by robots.txt: {next_url}"
            break

        progress["message"] = f"Loading listing: {next_url}"
        html=client.fetch(next_url)
        if not html:
            progress["message"] = f"Failed to load listing: {next_url}"
            break

        discovered=discover_profiles(html, next_url)
        progress["message"] = f"Found {len(discovered)} profile links on {next_url}"

        for p in discovered:
            if max_profiles is not None and len(results)>=max_profiles:
                progress["message"]="Reached max profiles"
                return results

            prof_url=p.get("profile_url") or next_url
            if prof_url in visited:
                continue
            visited.add(prof_url)

            if not client.allowed_by_robots(prof_url):
                progress["message"] = f"Robots blocked profile: {prof_url}"
                continue

            progress["message"] = f"Opening profile: {prof_url}"
            ph=client.fetch(prof_url)
            if not ph:
                progress["message"] = f"Failed to open profile: {prof_url}"
                continue

            name, school, research, email, phone = parse_profile(ph, prof_url, site_regdom)
            if not email:
                # Not a teaching-staff email or none found
                progress["message"] = f"Skipped (no staff email): {prof_url}"
                continue

            results.append({
                "name": name or p.get("name",""),
                "school": school, "research_area": research,
                "email": email, "phone": phone,
                "profile_url": prof_url,
            })
            progress["visited"] = progress.get("visited",0)+1

            if max_profiles is not None and len(results)>=max_profiles:
                progress["message"]="Reached max profiles"
                return results

        nxt=find_next_page(html,next_url)
        if not nxt or nxt==next_url:
            progress["message"] = "No further pages."
            break

        pages+=1
        next_url=nxt
        progress["message"] = f"Following pagination (page {pages+1})…"

    return results

def scrape_sites(sites: List[str], uni_label: str, max_profiles: Optional[int],
                 translate_en: bool, delay_min: float, delay_max: float, progress: Dict):
    client = HttpClient(delay_min, delay_max)  # JS forced ON
    translator = TranslatorWrapper() if translate_en else None
    rows=[]
    try:
        for site in sites:
            site=site.strip()
            if not site or not site.lower().startswith(("http://","https://")): continue
            site_rows = process_site(client, site, max_profiles, progress)
            uni = uni_label or infer_university_from_url(site)
            for r in site_rows:
                name=r.get("name",""); research=r.get("research_area","")
                if translator:
                    def needs_trans(s: str)->bool:
                        return bool(re.search(r"[\u0100-\uFFFF]", s)) and not re.search(r"[A-Za-z]{4,}", s)
                    if needs_trans(name): name=translator.translate(name,"en")
                    if needs_trans(research): research=translator.translate(research,"en")
                rows.append({
                    "university": uni,
                    "school": r.get("school",""),
                    "name": name,
                    "email": r.get("email",""),
                    "phone": r.get("phone",""),
                    "research_area": research,
                    "profile_url": r.get("profile_url",""),
                })
                if max_profiles is not None and len(rows)>=max_profiles:
                    progress["message"]="Reached global max"
                    break
            if max_profiles is not None and len(rows)>=max_profiles:
                break
    finally:
        client.close()
    # dedup
    seen=set(); dedup=[]
    for p in rows:
        key=(p["name"].lower(), p["profile_url"])
        if key in seen: continue
        seen.add(key); dedup.append(p)
    progress["done"]=True
    return dedup

# ----------------------------- Flask app -------------------------------------
app = Flask(__name__)
JOBS: Dict[str, Dict] = {}  # job_id → {"state","visited","limit","rows","message"}

INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Faculty Scraper (JS Enabled)</title>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial; margin:24px;}
    .card{max-width:1100px; margin:auto; padding:16px; border:1px solid #e5e7eb; border-radius:14px; box-shadow:0 2px 8px rgba(0,0,0,.04);}
    textarea,input,select{width:100%; padding:10px; border:1px solid #d1d5db; border-radius:10px;}
    label{font-weight:600; margin-top:10px; display:block;}
    .row{display:grid; grid-template-columns:1fr 1fr; gap:12px;}
    .btn{background:#111827;color:#fff;border:none;border-radius:10px;padding:10px 14px;cursor:pointer}
    .btn:disabled{opacity:.6;cursor:not-allowed}
    .muted{color:#6b7280}
    .progress{height:10px;background:#e5e7eb;border-radius:999px;overflow:hidden;margin:8px 0 4px;}
    .bar{height:100%;width:0;background:#2563eb;transition:width .3s}
    table{border-collapse:collapse; width:100%; margin-top:16px;}
    th,td{border:1px solid #e5e7eb;padding:8px;font-size:14px}
    th{background:#f9fafb; text-align:left;}
    tr.sel{background:#eef2ff;}
    .toolbar{display:flex; gap:8px; flex-wrap:wrap; margin:10px 0;}
    .pill{padding:8px 10px;border:1px solid #d1d5db;border-radius:999px;background:#fff;cursor:pointer}
    .right{display:flex;gap:8px;align-items:center}
    /* Add: visible status styles */
.info{background:#dbeafe;border:1px solid #bfdbfe;color:#1e3a8a;padding:8px 10px;border-radius:8px;margin-top:8px}
.error{background:#fee2e2;border:1px solid #fecaca;color:#7f1d1d;padding:8px 10px;border-radius:8px;margin-top:8px}

  </style>
</head>
<body>
  <div class="card">
    <h2>Faculty Scraper <span class="muted">(Developed by Shah Dad Hasil)</span></h2>
    <p class="muted">Paste one or more department/faculty listing URLs. Only <b>teaching staff emails</b> are kept.</p>
    <form id="frm">
      <label>Department Listing URLs (one per line)</label>
      <textarea id="sites" rows="5" placeholder="https://www.cs.cit.tum.de/en/cs/people/professors/"></textarea>
      <div class="row">
        <div>
          <label>University Label (optional)</label>
          <input id="unilabel" placeholder="Technical University of Munich"/>
        </div>
        <div>
          <label>Max Profiles (optional)</label>
          <input id="max" type="number" min="1" placeholder="50"/>
        </div>
      </div>
      <div class="row">
        <div><label><input type="checkbox" id="translate"/> Translate non-English fields</label></div>
        <div><label class="muted">JS rendering is always on</label></div>
      </div>
      <div class="row">
        <div><label>Delay Min (s)</label><input id="dmin" type="number" step="0.1" value="1.0"/></div>
        <div><label>Delay Max (s)</label><input id="dmax" type="number" step="0.1" value="2.5"/></div>
      </div>
      <div style="margin-top:12px; display:flex; gap:8px;">
        <button class="btn" id="runBtn" type="submit">Start Scrape</button>
        <span class="muted" id="msg"></span>
      </div>
    </form>

    <div id="prog" style="display:none; margin-top:12px;">
      <div class="progress"><div class="bar" id="bar"></div></div>
      <div class="right"><span id="ptext" class="muted">Starting…</span>
        <button class="pill" id="dlBtn" style="display:none;">Download Excel</button>
      </div>
      <div id="statusMsg"></div>
    </div>

    <div class="toolbar" id="tl" style="display:none;">
      <button class="pill" id="copyAll">Copy All</button>
      <select class="pill" id="colSel"></select>
      <button class="pill" id="copyCol">Copy Column</button>
      <button class="pill" id="copyRow" disabled>Copy Selected Row</button>
    </div>
    <div id="tableWrap"></div>
  </div>

<script>
const $ = (q)=>document.querySelector(q);

let JOB="", ROWS=[];
const COLS = ["university","school","name","email","phone","research_area"];

function mkTable(rows){
  const wrap = $("#tableWrap");
  if(!rows.length){ wrap.innerHTML = "<p class='muted'>No rows.</p>"; return; }
  let html = "<table><thead><tr>";
  for(const c of COLS){ html += `<th>${c}</th>`; }
  html += "</tr></thead><tbody>";
  rows.forEach((r,i)=>{
    html += `<tr data-i="${i}">`;
    for(const c of COLS){ html += `<td>${(r[c]||"")}</td>`; }
    html += "</tr>";
  });
  html += "</tbody></table>";
  wrap.innerHTML = html;

  wrap.querySelectorAll("tbody tr").forEach(tr=>{
    tr.addEventListener("click",()=>{
      wrap.querySelectorAll("tr.sel").forEach(x=>x.classList.remove("sel"));
      tr.classList.add("sel");
      $("#copyRow").disabled = false;
    });
  });

  const sel = $("#colSel");
  sel.innerHTML = COLS.map(c=>`<option value="${c}">${c}</option>`).join("");
}
function updateStatusMessage(message) {
  const statusDiv = $("#statusMsg");
  if (!message) {
    statusDiv.innerHTML = "";
    return;
  }
  
  const isError = message.toLowerCase().includes('error') || message.toLowerCase().includes('failed');
  statusDiv.innerHTML = `<div class="${isError ? 'error' : 'info'}">${message}</div>`;
}
async function startJob(payload){
  const res = await fetch("/start", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload)});
  if(!res.ok){ throw new Error("Failed to start"); }
  const j = await res.json();
  return j.job_id;
}

async function poll(){
  const res = await fetch(`/status/${JOB}`);
    if (!res.ok) {
    updateStatusMessage("Failed to get job status");
    return;
  }
  const st = await res.json();
  const limit = st.limit || 0;
  const visited = st.visited || 0;
  const done = !!st.done;
  const msg = st.message || "";
  updateStatusMessage(msg);

  $("#ptext").textContent = done ? `Done. ${visited} saved.` : (limit ? `Scraping… ${visited}/${limit}` : `Scraping… ${visited} saved`);
  const pct = limit ? Math.min(100, Math.round(visited/limit*100)) : (visited ? Math.min(100, visited) : 0);
  $("#bar").style.width = pct + "%";
 if(done){
    $("#dlBtn").style.display = "inline-block";
    const res2 = await fetch(`/results/${JOB}`);
    if (res2.ok) {
      const data = await res2.json();
      ROWS = data.rows || [];
      mkTable(ROWS);
      $("#tl").style.display = "flex";
    } else {
      updateStatusMessage("Failed to get results");
    }
    return;
  }
  setTimeout(poll, 1000);
}

$("#frm").addEventListener("submit", async (e)=>{
  e.preventDefault();
  $("#runBtn").disabled = true;
  $("#msg").textContent = "Starting…";
  $("#prog").style.display = "block";
  $("#dlBtn").style.display = "none";
  $("#tl").style.display = "none";
  $("#tableWrap").innerHTML = "";
  $("#copyRow").disabled = true;
  updateStatusMessage("");

  const payload = {
    sites: $("#sites").value.split(/\\r?\\n/).map(s=>s.trim()).filter(Boolean),
    university_label: $("#unilabel").value.trim(),
    max_profiles: $("#max").value ? parseInt($("#max").value, 10) : null,
    translate_en: $("#translate").checked,
    delay_min: parseFloat($("#dmin").value || "1.0"),
    delay_max: parseFloat($("#dmax").value || "2.5")
  };
    if (payload.sites.length === 0) {
    updateStatusMessage("Please enter at least one URL");
    $("#runBtn").disabled = false;
    return;
  }
  try{
    JOB = await startJob(payload);
    $("#msg").textContent = "Job: " + JOB;
    poll();
  }catch(err){
    updateStatusMessage("Failed to start job: " + err.message);
    $("#runBtn").disabled = false;
  }
});

$("#dlBtn").addEventListener("click", ()=>{
  window.location = `/download/${JOB}`;
});

async function copyText(txt){
  try{ await navigator.clipboard.writeText(txt); alert("Copied!"); }
  catch{ alert("Copy failed"); }
}

$("#copyAll").addEventListener("click", ()=>{
  if(!ROWS.length) return;
  const lines = [COLS.join("\\t")].concat(ROWS.map(r=>COLS.map(c=>r[c]||"").join("\\t")));
  copyText(lines.join("\\n"));
});

$("#copyCol").addEventListener("click", ()=>{
  if(!ROWS.length) return;
  const col = $("#colSel").value;
  const lines = [col].concat(ROWS.map(r=>r[col]||""));
  copyText(lines.join("\\n"));
});

$("#copyRow").addEventListener("click", ()=>{
  const sel = document.querySelector("#tableWrap tr.sel");
  if(!sel){ alert("Select a row first"); return; }
  const idx = parseInt(sel.getAttribute("data-i"),10);
  const r = ROWS[idx];
  copyText(COLS.map(c=>r[c]||"").join("\\t"));
});
</script>
</body>
</html>
"""

# ----------------------------- Routes ----------------------------------------
app = Flask(__name__)

@app.route("/", methods=["GET"])
def index():
    return render_template_string(INDEX_HTML)

@app.route("/start", methods=["POST"])
def start():
    data = request.get_json(force=True)
    sites: List[str] = data.get("sites", [])
    uni_label = data.get("university_label", "").strip()
    max_profiles = data.get("max_profiles", None)
    translate_en = bool(data.get("translate_en", False))
    delay_min = float(data.get("delay_min", 1.0))
    delay_max = float(data.get("delay_max", 2.5))

    job_id = uuid.uuid4().hex[:10]
    JOBS[job_id] = {"done": False, "visited": 0, "limit": max_profiles, "rows": [], "message": ""}

    def runner():
        progress = JOBS[job_id]
        try:
            progress["message"] = "Starting scrape…"
            rows = scrape_sites(
                sites=sites,
                uni_label=uni_label,
                max_profiles=max_profiles,
                translate_en=translate_en,
                delay_min=delay_min,
                delay_max=delay_max,
                progress=progress
            )
            # keep only rows that actually have email (already enforced), but double-guard:
            rows = [r for r in rows if r.get("email")]
            progress["rows"] = rows
            progress["visited"] = len(rows)
            progress["done"] = True
            progress["message"] = progress.get("message") or "Complete"
        except Exception as e:
            logging.exception("Job %s failed", job_id)
            progress["done"] = True
            progress["message"] = f"Error: {e}"

    t = threading.Thread(target=runner, daemon=True)
    t.start()

    return jsonify({"job_id": job_id})

@app.route("/status/<job_id>", methods=["GET"])
def status(job_id):
    st = JOBS.get(job_id)
    if not st:
        return jsonify({"error": "unknown job"}), 404
    return jsonify({"done": st.get("done"), "visited": st.get("visited",0), "limit": st.get("limit"), "message": st.get("message","")})

@app.route("/results/<job_id>", methods=["GET"])
def results(job_id):
    st = JOBS.get(job_id)
    if not st:
        return jsonify({"error": "unknown job"}), 404
    rows = st.get("rows", [])
    clean = [{k: r.get(k,"") for k in ["university","school","name","email","phone","research_area"]} for r in rows]
    return jsonify({"rows": clean})

@app.route("/download/<job_id>", methods=["GET"])
def download(job_id):
    st = JOBS.get(job_id)
    if not st:
        return "Unknown job", 404
    rows = st.get("rows", [])
    df = pd.DataFrame([{k: r.get(k,"") for k in ["university","school","name","email","phone","research_area"]} for r in rows])
    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="professors")
    bio.seek(0)
    return send_file(bio, as_attachment=True, download_name="professors.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ----------------------------- Main ------------------------------------------
if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", "8080"))
    app.run(debug=False, host="0.0.0.0", port=port)
