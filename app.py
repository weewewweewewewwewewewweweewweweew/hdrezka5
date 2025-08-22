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
# Усиленная настройка CORS, разрешающая запросы отовсюду
CORS(app, resources={r"/search-franchise": {"origins": "*"}})

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

    print(f"1. Выполняю AJAX-поиск для '{search_query}'...")
    response = session.post(search_ajax_url, data=payload, headers=post_headers, timeout=15)
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

# --- Функции парсинга ---
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
    response = session.get(url, headers=HEADERS, timeout=15)
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
                result = future.result() # Если здесь будет ошибка, она будет поймана в основном блоке
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

    try:
        print(f"\n--- Получен новый запрос: '{movie_title}' ---")
        
        with requests.Session() as session:
            session.get(BASE_URL, headers=HEADERS, timeout=15) # Получаем cookies
            
            initial_movie_url = find_movie_url_with_requests(session, movie_title)
            
            if not initial_movie_url:
                return jsonify({"error": f"Movie '{movie_title}' not found"}), 404

            print("2. Начинаю ускоренный сбор данных...")
            detailed_movies_list = process_franchise_concurrently(session, initial_movie_url)
            
            if detailed_movies_list:
                detailed_movies_list.sort(key=lambda x: str(x['year']))
                print("--- Запрос успешно обработан ---")
                return jsonify(detailed_movies_list)
            else:
                return jsonify({"error": "Could not extract franchise data"}), 500

    except requests.exceptions.Timeout:
        print("--- ! Ошибка: Сайт hdrezka не ответил вовремя. ---")
        return jsonify({"error": "The target site (HDRezka) timed out."}), 504 # Gateway Timeout
    except requests.exceptions.RequestException as e:
        print(f"--- ! Ошибка сети при обращении к hdrezka: {e} ---")
        return jsonify({"error": f"Network error when contacting HDRezka: {e}"}), 502 # Bad Gateway
    except Exception as e:
        # Эта секция поймает ЛЮБУЮ другую ошибку и вернет ее в JSON
        print(f"--- ! Произошла непредвиденная ошибка на сервере: {e} ---")
        return jsonify({"error": f"An unexpected server error occurred: {str(e)}"}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
