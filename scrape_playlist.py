import argparse
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import List, Optional

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
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
    chrome_options = Options()
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
    links = []
    seen = set()

    # Try the most common playlist list containers first.
    selectors = [
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
            if not href:
                continue
            if "/video/" not in href:
                continue
            if href in seen:
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
    # Try reading the current video source from the player.
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

    # Fallback: parse from the embedded flashvars in the page source.
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


def download_file(url: str, output_dir: Path, filename: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    file_path = output_dir / f"{sanitize_filename(filename)}.mp4"
    counter = 1
    while file_path.exists() and file_path.stat().st_size > 0:
        file_path = output_dir / f"{sanitize_filename(filename)}_{counter}.mp4"
        counter += 1

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": urllib.parse.urljoin(url, "/"),
        },
    )
    with urllib.request.urlopen(req, timeout=120) as response, open(file_path, "wb") as fh:
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


def scrape_playlist(playlist_url: str, output_dir: Path, headless: bool, max_videos: Optional[int], delay: float) -> None:
    driver = build_driver(output_dir, headless=headless)
    driver.get(playlist_url)

    try:
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    except Exception:
        pass

    video_links = collect_video_links(driver)
    if not video_links:
        print("Nenhum link de vídeo encontrado na playlist.")
        driver.quit()
        return

    print(f"Links encontrados: {len(video_links)}")

    playlist_handle = driver.current_window_handle
    processed = 0

    for video_url in video_links:
        if max_videos and processed >= max_videos:
            break

        try:
            driver.switch_to.new_window("tab")
            driver.get(video_url)
            time.sleep(2)

            title = get_page_title(driver)
            print(f"Baixando: {title}")

            video_url_to_download = None
            for quality_name in QUALITY_ORDER:
                if click_quality(driver, quality_name):
                    video_url_to_download = extract_video_url(driver)
                    if video_url_to_download and video_url_to_download.startswith("http"):
                        break
                    time.sleep(1)

            if not video_url_to_download:
                video_url_to_download = extract_video_url(driver)

            if video_url_to_download and video_url_to_download.startswith("http"):
                saved_path = download_file(video_url_to_download, output_dir, title)
                print(f"Salvo em: {saved_path}")
            else:
                print(f"Não foi possível obter a URL de vídeo para: {video_url}")

            driver.close()
            driver.switch_to.window(playlist_handle)
            processed += 1
            if delay > 0:
                time.sleep(delay)
        except Exception as exc:
            print(f"Erro ao processar {video_url}: {exc}")
            try:
                driver.close()
            except Exception:
                pass
            driver.switch_to.window(playlist_handle)
            processed += 1

    driver.quit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrapper de playlist com Selenium")
    parser.add_argument("playlist_url", help="URL da página da playlist")
    parser.add_argument("--output-dir", default=str(Path.home() / "Downloads"), help="Pasta para salvar os vídeos")
    parser.add_argument("--max-videos", type=int, default=None, help="Limite opcional de vídeos para baixar")
    parser.add_argument("--delay", type=float, default=2.0, help="Tempo em segundos entre cada vídeo")
    parser.add_argument("--headless", action="store_true", help="Usar o Chrome em modo headless")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser().resolve()
    scrape_playlist(args.playlist_url, output_dir, headless=args.headless, max_videos=args.max_videos, delay=args.delay)


if __name__ == "__main__":
    main()
