from datetime import datetime
import requests
import csv
import bs4

USER_AGENT= "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0"
REQUEST_HEADER = {
    'User-Agent': USER_AGENT,
    'Accept-Language': 'en-US,en;q=0.5',
}

def get_page_html(url):
    res = requests.get(url=url, headers=REQUEST_HEADER)
    return res.content


def get_job_title(soup):
    title = soup.find("h1", class_="job-detail__info--title").get_text(strip=True)
    return title

def get_job_exp(soup):
    sections = soup.find_all("div", class_="job-detail__box--left job-detail__info")
    for section in sections:
        title = section.find("div", class_="job-detail__info--section-content-title")
        value = section.find("div", class_="job-detail__info--section-content-value")
    return value.get_text(strip=True)
        

def extract_job_info(url):
    job_info = {}
    print(f"Scraping URL: {url}")
    html = get_page_html(url=url)
    soup = bs4.BeautifulSoup(html, 'lxml')
    job_info['exp'] = get_job_exp(soup)
    job_info['title'] = get_job_title(soup)
    return job_info

if __name__ == "__main__":
    job_list = []
    with open('topcv_job_urls.csv', newline='') as csvfile:
        reader =  csv.reader(csvfile, delimiter=',')
        for row in reader:
            url = row[0]
            job_list.append(extract_job_info(url))
    output_file_name = 'output-{}.csv'.format(
        datetime.today().strftime("%d-%m-%Y")
    )
    with open(output_file_name, 'w', encoding="utf-8") as outputfile:
        writer = csv.writer(outputfile)
        writer.writerow(job_list[0].keys())
        for job in job_list:
            writer.writerow(job.values())