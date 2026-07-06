import argparse
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import List, Optional

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


QUALITY_ORDER = ["720p", "480p"]
QUALITY_SELECTORS = {
    "720p": ["a[data-format='3']", "a[data-format='4']", "[data-quality='720p']", "a:contains('720p')"],
    "480p": ["a[data-format='2']", "[data-quality='480p']", "a:contains('480p')"],
}


def sanitize_filename(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return value or "video"


def build_driver(download_dir: Path, headless: bool) -> webdriver.Chrome:
    chrome_options = ChromeOptions()
    if headless:
        chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--user-agent=Mozilla/5.0")

    prefs = {
        "download.default_directory": str(download_dir),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
        "profile.default_content_settings.popups": 0,
    }
    chrome_options.add_experimental_option("prefs", prefs)
    chrome_options.add_experimental_option("excludeSwitches", ["enable-logging"])
    service = ChromeService(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=chrome_options)


def get_page_title(driver: webdriver.Chrome) -> str:
    try:
        title = driver.title.strip()
    except Exception:
        title = "video"
    return title or "video"


def collect_video_links(driver: webdriver.Chrome) -> List[str]:
    links: List[str] = []
    seen = set()

    selectors = [
        "#playlist_view_playlist_view_items a.th[data-playlist-item]",
        "#playlist_view_playlist_view_items a[href*='/video/']",
        "div.thumbs a[href*='/video/']",
        "a.th[href*='/video/']",
        "a[href*='/video/']",
    ]

    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except WebDriverException:
            elements = []

        for element in elements:
            href = (element.get_attribute("href") or "").strip()
            if not href or "/video/" not in href or href in seen:
                continue
            seen.add(href)
            links.append(href)

    if links:
        return links

    page_source = driver.page_source
    matches = re.findall(r'https?://[^"\'\s<>]+/video/[^"\'\s<>]+', page_source)
    for href in matches:
        if href not in seen:
            seen.add(href)
            links.append(href)

    return links


def click_next_playlist_page(driver: webdriver.Chrome) -> bool:
    try:
        next_link = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, ".pagination .next a[data-action='ajax'], .playlist_page_numbers .next a[data-action='ajax']"))
        )
        before_html = driver.find_element(By.CSS_SELECTOR, "#playlist_view_playlist_view_items").get_attribute("innerHTML")
        next_link.click()
        WebDriverWait(driver, 10).until(
            lambda d: d.find_element(By.CSS_SELECTOR, "#playlist_view_playlist_view_items").get_attribute("innerHTML") != before_html
        )
        time.sleep(0.5)
        return True
    except Exception:
        return False


def save_video_index(index_file: Path, entries: List[dict]) -> None:
    index_file.parent.mkdir(parents=True, exist_ok=True)
    index_file.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def load_video_index(index_file: Path) -> List[dict]:
    candidates = [index_file]
    candidates.append(index_file.with_name("data.json"))
    candidates.append(index_file.with_name("videos.json"))

    for candidate in candidates:
        if candidate.exists():
            try:
                content = json.loads(candidate.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue

            if isinstance(content, list):
                result = []
                for item in content:
                    if isinstance(item, dict) and item.get("url"):
                        result.append({
                            "url": str(item["url"]).strip(),
                            "downloaded": bool(item.get("downloaded", False)),
                        })
                    elif isinstance(item, str) and item.strip():
                        result.append({"url": item.strip(), "downloaded": False})
                return result

    return []


def collect_playlist_links(playlist_url: str, index_file: Path, headless: bool, delay: float, max_pages: Optional[int]) -> List[dict]:
    driver = build_driver(index_file.parent, headless=headless)
    driver.get(playlist_url)

    try:
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    except Exception:
        pass

    entries: List[dict] = []
    seen_urls = set()
    page_number = 0

    while True:
        if max_pages and page_number >= max_pages:
            break

        page_number += 1
        print(f"Coletando página {page_number}")

        for video_url in collect_video_links(driver):
            if video_url not in seen_urls:
                seen_urls.add(video_url)
                entries.append({"url": video_url, "downloaded": False})

        if not click_next_playlist_page(driver):
            break

        time.sleep(max(0.1, delay / 4))

    save_video_index(index_file, entries)
    print(f"{len(entries)} links salvos em {index_file}")
    driver.quit()
    return entries


def find_quality_link(driver: webdriver.Chrome, quality_name: str):
    for selector in QUALITY_SELECTORS.get(quality_name, []):
        try:
            if selector.startswith("a[") or selector.startswith("["):
                element = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                )
                return element
        except TimeoutException:
            continue
        except Exception:
            continue

    return None


def click_quality(driver: webdriver.Chrome, quality_name: str) -> bool:
    try:
        gear = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".fp-settings")))
        gear.click()
        time.sleep(0.8)
    except Exception:
        pass

    quality_element = find_quality_link(driver, quality_name)
    if quality_element is None:
        return False

    try:
        quality_element.click()
        time.sleep(2)
        return True
    except Exception:
        return False


def extract_video_url(driver: webdriver.Chrome) -> Optional[str]:
    for script in [
        "return document.querySelector('video.fp-engine')?.currentSrc || document.querySelector('video.fp-engine')?.src || ''",
        "return document.querySelector('video')?.currentSrc || document.querySelector('video')?.src || ''",
    ]:
        try:
            src = driver.execute_script(script)
            if src and src.startswith("http"):
                return src
        except Exception:
            continue

    patterns = [
        r"video_alt_url3\s*:\s*'([^']+)'",
        r"video_alt_url2\s*:\s*'([^']+)'",
        r"video_alt_url\s*:\s*'([^']+)'",
        r"video_url\s*:\s*'([^']+)'",
    ]
    page_source = driver.page_source
    for pattern in patterns:
        match = re.search(pattern, page_source)
        if match:
            return match.group(1)

    return None


def download_file(url: str, output_dir: Path, filename: str, retries: int = 3) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    file_path = output_dir / f"{sanitize_filename(filename)}.mp4"
    counter = 1
    while file_path.exists() and file_path.stat().st_size > 0:
        file_path = output_dir / f"{sanitize_filename(filename)}_{counter}.mp4"
        counter += 1

    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": urllib.parse.urljoin(url, "/"),
                },
            )
            with urllib.request.urlopen(req, timeout=60) as response, open(file_path, "wb") as fh:
                total_size = int(response.headers.get("Content-Length", "0") or 0)
                downloaded = 0

                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    downloaded += len(chunk)

                    if total_size > 0:
                        percent = min(100, int(downloaded / total_size * 100))
                        bar_length = 40
                        filled = int(bar_length * percent / 100)
                        bar = "#" * filled + "-" * (bar_length - filled)
                        mb_done = downloaded / (1024 * 1024)
                        mb_total = total_size / (1024 * 1024)
                        print(f"\rBaixando {filename}: [{bar}] {percent}% ({mb_done:.1f}/{mb_total:.1f} MB)", end="", flush=True)
                    else:
                        mb_done = downloaded / (1024 * 1024)
                        print(f"\rBaixando {filename}: {mb_done:.1f} MB", end="", flush=True)

            print()
            return file_path
        except Exception as exc:
            if attempt == retries:
                raise exc
            print(f"\nFalha no download de {filename} (tentativa {attempt}/{retries}): {exc}")
            time.sleep(3)

    raise RuntimeError(f"Falha ao baixar {filename}")


def download_from_index(index_file: Path, output_dir: Path, headless: bool, delay: float, max_videos: Optional[int]) -> None:
    entries = load_video_index(index_file)
    if not entries:
        print(f"Nenhum link encontrado em {index_file}.")
        return

    driver = build_driver(output_dir, headless=headless)
    playlist_handle = driver.current_window_handle

    pending_entries = [entry for entry in entries if not entry.get("downloaded", False)]
    if max_videos:
        pending_entries = pending_entries[:max_videos]

    for index, entry in enumerate(pending_entries, start=1):
        video_url = entry.get("url", "").strip()
        if not video_url:
            continue

        try:
            driver.switch_to.new_window("tab")
            driver.get(video_url)
            time.sleep(0.4)

            title = get_page_title(driver)
            print(f"[{index}/{len(entries)}] Baixando: {title}")

            video_url_to_download = None
            for quality_name in QUALITY_ORDER:
                if click_quality(driver, quality_name):
                    video_url_to_download = extract_video_url(driver)
                    if video_url_to_download and video_url_to_download.startswith("http"):
                        break
                    time.sleep(0.2)

            if not video_url_to_download:
                video_url_to_download = extract_video_url(driver)

            if video_url_to_download and video_url_to_download.startswith("http"):
                saved_path = download_file(video_url_to_download, output_dir, title)
                print(f"Salvo em: {saved_path}")
                for item in entries:
                    if item.get("url") == video_url:
                        item["downloaded"] = True
                        break
                save_video_index(index_file, entries)
            else:
                print(f"Não foi possível obter a URL de vídeo para: {video_url}")

            driver.close()
            driver.switch_to.window(playlist_handle)
            if delay > 0:
                time.sleep(max(0.1, delay / 5))
        except Exception as exc:
            print(f"Erro ao processar {video_url}: {exc}")
            try:
                driver.close()
            except Exception:
                pass
            driver.switch_to.window(playlist_handle)

    driver.quit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrapper de playlist com Selenium")
    parser.add_argument("playlist_url", help="URL da página da playlist")
    parser.add_argument("--output-dir", default=str(Path.home() / "Downloads"), help="Pasta para salvar os vídeos")
    parser.add_argument("--index-file", default="videos.json", help="Arquivo JSON para salvar/ler os links dos vídeos")
    parser.add_argument("--max-videos", type=int, default=None, help="Limite opcional de vídeos para baixar")
    parser.add_argument("--max-pages", type=int, default=None, help="Limite opcional de páginas da playlist para coletar")
    parser.add_argument("--delay", type=float, default=2.0, help="Tempo em segundos entre cada vídeo")
    parser.add_argument("--headless", action="store_true", help="Usar o Chrome em modo headless")
    parser.add_argument("--collect-only", action="store_true", help="Só coleta os links e salva em JSON")
    parser.add_argument("--download-only", action="store_true", help="Só baixa os vídeos a partir do arquivo JSON")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser().resolve()
    index_file = Path(args.index_file).expanduser().resolve()

    if args.collect_only and args.download_only:
        raise SystemExit("Escolha apenas uma das opções: --collect-only ou --download-only")

    if args.download_only:
        download_from_index(index_file, output_dir, headless=args.headless, delay=args.delay, max_videos=args.max_videos)
    else:
        collect_playlist_links(args.playlist_url, index_file, headless=args.headless, delay=args.delay, max_pages=args.max_pages)
        if not args.collect_only:
            download_from_index(index_file, output_dir, headless=args.headless, delay=args.delay, max_videos=args.max_videos)


if __name__ == "__main__":
    main()
