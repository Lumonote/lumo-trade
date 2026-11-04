"""确定性成分与矩阵交互验收；使用真实聚合代码及页面，数据为隔离测试夹具。"""
import copy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit

from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright, expect, Error

from analysis.short_term_screen import build_report
from test_short_term_memberships import source, DAY

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/"results/short_term_memberships"
OUT.mkdir(parents=True,exist_ok=True)
data=source()
data["sectors"]["memberships"]["BK0002"]={"date":DAY,"complete":True,"source":"测试真实成分接口契约",
    "members":[{"code":"600004","name":"成分丁"},{"code":"600005","name":"成分戊"}]}
data["sectors"]["rows"].append({"code":"BK0003","name":"待补成分","type":"行业","net":1e8,"pct":1})
snapshot=build_report(data,DAY,DAY+"T10:02",current_only=True)
snapshot.update(available=True,historical=False)
env=Environment(loader=FileSystemLoader(str(ROOT/"webui/templates")))
html=env.get_template("short_term_screen.html").render(desktop_mode=False,asset_v=lambda name:"test")


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if urlsplit(self.path).path=="/desktop/short_term":
            self.send_response(200);self.send_header("Content-Type","text/html;charset=utf-8");self.end_headers();self.wfile.write(html.encode());return
        super().do_GET()
    def log_message(self,*args):pass


server=ThreadingHTTPServer(("127.0.0.1",0),partial(Handler,directory=str(ROOT/"webui")))
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
    with sync_playwright() as p:
        try:browser=p.chromium.launch(headless=True,args=["--enable-unsafe-swiftshader"])
        except Error:browser=p.chromium.launch(headless=True,channel="chrome",args=["--enable-unsafe-swiftshader"])
        page=browser.new_page(viewport={"width":1920,"height":1080})
        errors=[]
        page.on("pageerror",lambda e:errors.append(str(e)))
        page.add_init_script("window.__feedCallbacks={};const originalTimeout=setTimeout;window.setTimeout=(fn,delay,...args)=>{const id=originalTimeout(fn,delay,...args);if([4200,17300].includes(delay))window.__feedCallbacks[delay]=()=>{clearTimeout(id);return fn(...args);};return id;};")
        page.route("**/api/short-term/overview*",lambda r:r.fulfill(json=snapshot))
        page.route("**/api/short-term/orders*",lambda r:r.fulfill(json={"big_buys":[],"big_sells":[],"refreshing":False}))
        def feed(route):
            kind=parse_qs(urlsplit(route.request.url).query)["kind"][0]
            if kind=="news":
                data["jinshi"].append({"title":"半导体新增直接消息","time":DAY+" 10:02"})
            elif kind=="limits":
                data["limits"]["events"].append({"code":"600003","name":"示例丙","date":DAY,"kind":"D"})
            else:
                route.fulfill(json={"changed":False});return
            updated=build_report(data,DAY,DAY+"T10:03",current_only=True)
            from webui.services.short_term_screen_service import FEED_FIELDS
            route.fulfill(json={"changed":True,"version":kind,"revision":1 if kind=="news" else 2,
                                "payload":{key:updated[key] for key in FEED_FIELDS[kind]}})
        page.route("**/api/short-term/feed*",feed)
        page.goto(f"http://127.0.0.1:{server.server_port}/desktop/short_term",wait_until="networkidle")
        expect(page.locator("#stScreen")).to_have_attribute("aria-busy","false")
        def cell(code,key):return page.locator(f'[data-st-kind="signal"][data-st-id="sector:{code}|{key}"]')
        expect(cell("BK0001","资金流入")).to_have_text("+2.5")
        expect(cell("BK0002","资金流入")).to_have_text("-3")
        expect(cell("BK0001","拉抬")).to_have_text("1*")
        expect(cell("BK0001","跌停")).to_have_text("0")
        expect(cell("BK0003","涨停")).to_have_text("—")
        expect(page.locator("#stGraphMeta")).to_contain_text("完整成分 2/3")
        expect(page.locator("#stNodes .st-node-stock")).to_have_count(4)
        assert page.locator("#stScene").evaluate("el=>el.__stMeteorCount")==7,"3条上联+4条真实成分线"
        expect(page.locator("#stStockCount")).to_have_text("2")
        cell("BK0001","涨停").click()
        expect(page.locator("#stDrawerTitle")).to_contain_text("涨停依据")
        expect(page.locator("#stDrawerBody")).to_contain_text("命中股票 · 1只")
        expect(page.locator('#stDrawerBody [data-st-kind="stock"]')).to_have_count(1)
        page.locator('#stDrawerBody [data-st-kind="stock"]').click()
        expect(page.locator("#stDrawerBody")).to_contain_text("600001")
        page.locator("#stDrawerClose").click()
        cell("BK0003","涨停").click()
        expect(page.locator("#stDrawerBody")).to_contain_text("数据未取得，不能解释为0")
        page.locator("#stDrawerClose").click()
        page.evaluate("window.__feedCallbacks[4200]()")
        expect(cell("BK0001","消息提及")).to_have_text("2*")
        page.evaluate("window.__feedCallbacks[17300]()")
        expect(cell("BK0001","跌停")).to_have_text("1")
        cell("BK0001","消息提及").click()
        expect(page.locator("#stDrawerBody")).to_contain_text("半导体新增直接消息")
        expect(page.locator("#stDrawerBody")).not_to_contain_text("泛题材信息")
        page.locator("#stDrawerClose").click()
        page.locator("#stMotion").click()
        for width,height in ((1920,1080),(1440,900),(390,844)):
            page.set_viewport_size({"width":width,"height":height})
            page.screenshot(path=str(OUT/f"matrix-{width}.png"),full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth+1")
        assert not errors,errors
        browser.close()
        print(json.dumps({"passed":True,"checks":["真实成分连线","单位与零/缺失","精确信号依据","消息与涨跌停独立更新","1920/1440/390布局"]},ensure_ascii=False))
finally:
    server.shutdown();server.server_close()
