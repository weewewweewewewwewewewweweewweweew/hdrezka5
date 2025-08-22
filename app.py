import re
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, urljoin

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

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

# --- Функции управления браузером ---
def start_lightweight_browser():
    """Запускает один экземпляр браузера для всего API."""
    print("--- Инициализация Selenium Driver ---")
    options = webdriver.ChromeOptions()
    options.page_load_strategy = 'eager'
    # Эти аргументы КРИТИЧЕСКИ важны для работы в Docker-контейнере Railway
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--blink-settings=imagesEnabled=false")
    options.add_argument(f"user-agent={HEADERS['User-Agent']}")
    options.add_experimental_option('excludeSwitches', ['enable-logging'])

    try:
        service = ChromeService()
        driver = webdriver.Chrome(service=service, options=options)
        print("--- Браузер успешно запущен ---")
        return driver
    except Exception as e:
        print(f"--- ! Не удалось запустить браузер: {e} ---")
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
    except WebDriverException as e:
        print(f"   Произошла ошибка WebDriver во время поиска: {e}")
        return None
    except Exception as e:
        print(f"   Произошла общая ошибка во время поиска: {e}")
        return None

# --- Функции парсинга (используют BeautifulSoup) ---
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

# --- НОВАЯ функция сбора данных через Selenium ---
def process_franchise_with_selenium(driver, start_url: str) -> list:
    """Собирает данные о франшизе, перемещаясь по ссылкам в ОДНОМ браузере."""
    urls_to_visit = [start_url]
    visited_urls = set()
    final_results = []

    while urls_to_visit:
        current_url = urls_to_visit.pop(0)
        if current_url in visited_urls:
            continue
        
        try:
            print(f"   - Перехожу на страницу: {current_url}")
            driver.get(current_url)
            visited_urls.add(current_url)
            
            # Ждем загрузки ключевого элемента
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".b-post__title"))
            )

            html_content = driver.page_source
            details = get_movie_details(html_content)
            final_results.append(details)
            
            soup = BeautifulSoup(html_content, 'lxml')
            franchise_items = soup.select('.b-post__partcontent_item a, .b-post__see_also_item a')
            
            for item in franchise_items:
                if item.has_attr('href'):
                    full_link = urljoin(BASE_URL, item['href'])
                    if full_link not in visited_urls:
                        urls_to_visit.append(full_link)
        
        except (WebDriverException, TimeoutException) as e:
            print(f"   ! Ошибка при обработке URL {current_url}: {e}")
            continue

    return final_results

# --- API Endpoint ---
@app.route('/search-franchise', methods=['GET'])
def search_franchise():
    movie_title = request.args.get('q')
    if not movie_title:
        return jsonify({"error": "Query parameter 'q' is required"}), 400

    print(f"\n--- Получен новый запрос: '{movie_title}' ---")
    
    initial_movie_url = search_with_persistent_browser(driver, movie_title)
    
    if not initial_movie_url:
        response = jsonify({"error": f"Movie '{movie_title}' not found"})
        response.headers['Content-Type'] = 'application/json; charset=utf-8'
        return response, 404

    print("3. Начинаю сбор данных о франшизе через Selenium...")
    try:
        detailed_movies_list = process_franchise_with_selenium(driver, initial_movie_url)
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
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
