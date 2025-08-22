import requests
import re
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from flask import Flask, request, jsonify
from flask_cors import CORS
import os

# --- Flask App Initialization ---
app = Flask(__name__)
CORS(app) # Разрешаем кросс-доменные запросы

# --- Константы и Настройки ---
BASE_URL = 'https://hdrezka.ag'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36'
}
MAX_WORKERS = 8

# --- Функции управления браузером ---

def start_lightweight_browser():
    """Запускает один экземпляр браузера для всего API."""
    print("--- Инициализация Selenium Driver ---")
    options = webdriver.ChromeOptions()
    options.page_load_strategy = 'eager'
    prefs = {"profile.managed_default_content_settings.images": 2}
    options.add_experimental_option("prefs", prefs)
    # Эти аргументы КРИТИЧЕСКИ важны для работы в Docker-контейнере Railway
    options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--blink-settings=imagesEnabled=false")
    options.add_argument(f"user-agent={HEADERS['User-Agent']}")

    try:
        # Указываем путь к chromedriver, который будет установлен через nixpacks.toml
        service = ChromeService()
        driver = webdriver.Chrome(service=service, options=options)
        print("--- Браузер успешно запущен ---")
        return driver
    except Exception as e:
        print(f"--- ! Не удалось запустить браузер: {e} ---")
        # Попробуем без webdriver-manager, т.к. в Railway он может не работать
        try:
            print("--- Попытка запуска браузера без webdriver-manager ---")
            driver = webdriver.Chrome(options=options)
            print("--- Браузер успешно запущен (вторая попытка) ---")
            return driver
        except Exception as e2:
            print(f"--- ! Вторая попытка запуска браузера провалилась: {e2} ---")
            return None

# Инициализируем драйвер один раз при старте приложения
driver = start_lightweight_browser()

def search_with_persistent_browser(driver, search_query: str) -> str | None:
    """Выполняет поиск, используя уже запущенный экземпляр браузера."""
    if not driver:
        print("Драйвер не инициализирован. Поиск невозможен.")
        return None
    try:
        wait = WebDriverWait(driver, 15)
        encoded_query = quote_plus(search_query)
        search_url = f"{BASE_URL}/search/?do=search&subaction=search&q={encoded_query}"
        
        print(f"1. Выполняю поиск '{search_query}'...")
        driver.get(search_url)

        print("2. Ищу ссылку на первый результат...")
        first_result_selector = "div.b-content__inline_item-link a"
        first_result_link = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, first_result_selector)))
        
        movie_url = first_result_link.get_attribute('href')
        print(f"   Найдена основная страница фильма: {movie_url}")
        return movie_url
    except TimeoutException:
        print(f"   Не найдено результатов по запросу '{search_query}'.")
        return None
    except Exception as e:
        print(f"   Произошла ошибка во время поиска: {e}")
        return None

# --- Функции парсинга (без изменений) ---

def get_movie_details(html_content: str) -> dict:
    soup = BeautifulSoup(html_content, 'lxml')
    details = {'english_title': 'N/A', 'year': 'N/A'}
    title_div = soup.find('div', class_='b-post__origtitle')
    if title_div:
        details['english_title'] = title_div.get_text(strip=True)
    info_table = soup.find('table', class_='b-post__info')
    if info_table:
        year_row = info_table.find(lambda tag: tag.name == 'tr' and ('Год' in tag.get_text() or 'Дата выхода' in tag.get_text()))
        if year_row:
            year_tag = year_row.find_all('td')[-1]
            if year_tag:
                year_match = re.search(r'\d{4}(?:-\d{4})?', year_tag.get_text())
                if year_match:
                    details['year'] = year_match.group(0)
    return details

def fetch_details_and_links(session, url: str) -> dict:
    response = session.get(url, timeout=10)
    response.raise_for_status()
    html = response.text
    details = get_movie_details(html)
    soup = BeautifulSoup(html, 'lxml')
    new_links = set()
    franchise_items = soup.select('.b-post__partcontent_item a, .b-post__see_also_item a')
    for item in franchise_items:
        if item.has_attr('href'):
            new_links.add(urljoin(BASE_URL, item['href']))
    return {'details': details, 'new_links': new_links}

def process_franchise_concurrently(start_url: str) -> list:
    final_results = []
    submitted_urls = {start_url}
    with requests.Session() as session:
        session.headers.update(HEADERS)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(fetch_details_and_links, session, start_url)}
            while futures:
                for future in as_completed(futures):
                    futures.remove(future)
                    try:
                        result = future.result()
                    except Exception as e:
                        print(f"  ! Задача для URL завершилась с ошибкой: {e}")
                        continue
                    final_results.append(result['details'])
                    for link in result['new_links']:
                        if link not in submitted_urls:
                            submitted_urls.add(link)
                            new_future = executor.submit(fetch_details_and_links, session, link)
                            futures.add(new_future)
                    break 
    return final_results

# --- API Endpoint ---

@app.route('/search-franchise', methods=['GET'])
def search_franchise():
    movie_title = request.args.get('q')
    if not movie_title:
        return jsonify({"error": "Query parameter 'q' is required"}), 400

    print(f"\n--- Получен новый запрос: '{movie_title}' ---")
    
    # Фаза 1: Используем Selenium для поиска
    initial_movie_url = search_with_persistent_browser(driver, movie_title)
    
    if not initial_movie_url:
        return jsonify({"error": f"Movie '{movie_title}' not found"}), 404

    # Фаза 2: Ускоренный парсинг с requests
    print("3. Начинаю ускоренный сбор данных...")
    try:
        detailed_movies_list = process_franchise_concurrently(initial_movie_url)
        if detailed_movies_list:
            detailed_movies_list.sort(key=lambda x: str(x['year']))
            print("--- Запрос успешно обработан ---")
            return jsonify(detailed_movies_list)
        else:
            return jsonify({"error": "Could not extract franchise data"}), 500
    except Exception as e:
        print(f"--- Ошибка на этапе парсинга: {e} ---")
        return jsonify({"error": "An error occurred during parsing"}), 500

if __name__ == "__main__":
    # Gunicorn будет управлять этим, но для локального теста можно запустить так
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)