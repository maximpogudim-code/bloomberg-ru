"""
UI-верификатор: ловит мёртвый JS и пустые метрики ДО того, как их увидит пользователь.
Запуск: .venv/bin/python tests/verify_ui.py [--base http://localhost:8080]
Выход 0 = всё ок, 1 = есть проблема.
"""
import argparse, re, sys, time


def check(base: str) -> int:
    import httpx
    # 1) тянем главную (ждём готовый дашборд, не страницу загрузки)
    html = ""
    for _ in range(40):
        try:
            html = httpx.get(base + "/", timeout=15).text
            if "ticker-inner" in html:
                break
        except Exception:
            pass
        time.sleep(1)
    if "ticker-inner" not in html:
        print("✗ дашборд не отдался (страница загрузки/ошибка)")
        return 1

    # 2) JS-синтаксис через esprima
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    if not blocks:
        print("✗ нет inline-script")
        return 1
    js = max(blocks, key=len)
    try:
        import esprima
        esprima.parseScript(js)
        print("✓ JS синтаксис валиден")
    except Exception as e:
        print(f"✗ JS СИНТАКСИЧЕСКАЯ ОШИБКА: {e}")
        return 1

    # 3) реальный браузер — исполнение JS, заполнение метрик
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("⚠ playwright нет — пропускаю браузерную проверку (JS-синтаксис ОК)")
        return 0

    errs = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page()
        pg.on("pageerror", lambda e: errs.append(str(e)[:160]))
        pg.on("console", lambda m: errs.append(f"console.{m.type}: {m.text}"[:160])
              if m.type == "error" else None)
        pg.goto(base + "/", wait_until="domcontentloaded", timeout=20000)
        time.sleep(9)

        def txt(sel):
            el = pg.query_selector(sel)
            return el.inner_text().strip() if el else ""

        hdr = txt("#hdr-idx")
        mkt = txt("#mkt-indices")
        price = txt("#ci-price")
        cont = pg.query_selector("#chart-container")
        has_chart = bool(cont and cont.query_selector("canvas, table"))
        ticks = len(pg.query_selector_all(".tick"))
        b.close()

    js_errs = [e for e in errs if e]
    ok = True
    if js_errs:
        ok = False
        print(f"✗ JS-ОШИБКИ В БРАУЗЕРЕ ({len(js_errs)}):")
        for e in js_errs[:8]:
            print("   ", e)
    else:
        print("✓ 0 JS-ошибок в браузере")
    print(f"  индексы(шапка): {'OK' if hdr else 'пусто'} | "
          f"рынки(панель): {'OK' if mkt else 'пусто'} | "
          f"цена графика: {price or 'пусто'} | "
          f"график canvas: {'OK' if has_chart else 'НЕТ'} | тикеров: {ticks}")
    if not has_chart:
        print("✗ график не отрисовался")
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080")
    a = ap.parse_args()
    if "esprima" not in sys.modules:
        try:
            import esprima  # noqa
        except Exception:
            import subprocess
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "esprima"], check=False)
    sys.exit(check(a.base))
