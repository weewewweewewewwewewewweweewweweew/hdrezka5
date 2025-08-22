import requests
import re
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, request, jsonify
from flask_cors import CORS
import os

# --- Flask App Initialization ---
app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False
CORS(app)

# --- Константы и Настройки ---
BASE_URL = 'https://hdrezka.ag'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7'
}
MAX_WORKERS = 8

# --- Функция поиска через requests ---
def find_movie_url_with_requests(session, search_query: str) -> str | None:
    search_ajax_url = urljoin(BASE_URL, "/ajax/search/")
    payload = {'q': search_query}
    
    post_headers = HEADERS.copy()
    post_headers['Referer'] = BASE_URL + '/'
    post_headers['X-Requested-With'] = 'XMLHttpRequest'

    try:
        print(f"1. Выполняю AJAX-поиск для '{search_query}'...")
        response = session.post(search_ajax_url, data=payload, headers=post_headers, timeout=10)
        response.raise_for_status()
        
        html_results = response.text
        if not html_results.strip():
             print(f"   Пустой ответ от AJAX поиска для '{search_query}'.")
             return None

        soup = BeautifulSoup(html_results, 'lxml')
        link_tag = soup.select_one("li.b-search__section_item a")
        
        if link_tag and link_tag.has_attr('href'):
            movie_url = link_tag['href']
            print(f"   Найдена основная страница фильма: {movie_url}")
            return movie_url
        else:
            print(f"   Не найдено результатов по запросу '{search_query}'.")
            return None
    except requests.RequestException as e:
        print(f"   Произошла ошибка во время поиска (requests): {e}")
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
    response = session.get(url, headers=HEADERS, timeout=10)
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

def process_franchise_concurrently(session, start_url: str) -> list:
    final_results = []
    submitted_urls = {start_url}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_details_and_links, session, start_url)}
        while futures:
            for future in as_completed(futures):
                futures.remove(future)
                try:
                    result = future.result()
                except Exception as e:
                    print(f"  ! Задача для URL {future} завершилась с ошибкой: {e}")
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
    
    with requests.Session() as session:
        # --- ВОТ САМАЯ ГЛАВНАЯ НОВАЯ СТРОКА ---
        session.get(BASE_URL, headers=HEADERS, timeout=10) # Получаем cookies с главной страницы
        # ------------------------------------

        initial_movie_url = find_movie_url_with_requests(session, movie_title)
        
        if not initial_movie_url:
            return jsonify({"error": f"Movie '{movie_title}' not found"}), 404

        print("2. Начинаю ускоренный сбор данных...")
        try:
            detailed_movies_list = process_franchise_concurrently(session, initial_movie_url)
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
    port = int(os
