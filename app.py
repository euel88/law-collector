"""
법제처 법령 수집기 - Streamlit 버전 (API 기반 최종 수정)
GitHub/Streamlit Cloud에서 실행 가능한 웹 애플리케이션
- 정교한 필터링 로직을 적용하여 장/절 제목 및 빈 조문 제거
"""

import streamlit as st
import requests
import xml.etree.ElementTree as ET
import json
import time
import re
from datetime import datetime
from bs4 import BeautifulSoup
import urllib3
import base64

# SSL 경고 무시
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 페이지 설정
st.set_page_config(
    page_title="법제처 법령 수집기 (최종 수정 버전)",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="expanded"
)

# 세션 상태 초기화
if 'collected_laws' not in st.session_state:
    st.session_state.collected_laws = {}
if 'collected_hierarchy' not in st.session_state:
    st.session_state.collected_hierarchy = {}
if 'collected_precs' not in st.session_state:
    st.session_state.collected_precs = []
if 'search_results' not in st.session_state:
    st.session_state.search_results = []
if 'selected_laws' not in st.session_state:
    st.session_state.selected_laws = []


class LawCollectorStreamlit:
    """Streamlit용 법령 수집기 (API 기반)"""

    def __init__(self):
        self.law_search_url = "http://www.law.go.kr/DRF/lawSearch.do"
        self.law_detail_url = "http://www.law.go.kr/DRF/lawService.do"
        self.prec_search_url = "http://www.law.go.kr/DRF/lawPrecSearch.do"
        self.delay = 0.3  # API 호출 간격

    def search_law(self, oc_code: str, law_name: str):
        """법령 검색"""
        params = {
            'OC': oc_code,
            'target': 'law',
            'type': 'XML',
            'query': law_name,
            'display': '100',
            'page': '1'
        }
        try:
            response = requests.get(
                self.law_search_url,
                params=params,
                timeout=10,
                verify=False
            )
            response.encoding = 'utf-8'
            if response.status_code != 200:
                st.error(f"API 응답 오류: HTTP {response.status_code}")
                return []

            content = response.text
            if content.strip().startswith('<!DOCTYPE') or content.strip().startswith('<html'):
                st.error("API가 HTML을 반환했습니다. 기관코드(OC)를 확인해주세요.")
                return []
            if content.startswith('\ufeff'):
                content = content[1:]

            root = ET.fromstring(content.encode('utf-8'))
            laws = []
            for law_elem in root.findall('.//law'):
                law_id = law_elem.findtext('법령ID', '')
                law_name_full = law_elem.findtext('법령명한글', '')
                if law_id and law_name_full:
                    law_info = {
                        'law_id': law_id,
                        'law_name': law_name_full,
                        'law_type': law_elem.findtext('법종구분', ''),
                        'promulgation_date': law_elem.findtext('공포일자', ''),
                        'enforcement_date': law_elem.findtext('시행일자', ''),
                    }
                    laws.append(law_info)
            return laws
        except ET.ParseError:
            st.error("XML 파싱 오류가 발생했습니다. 기관코드(OC)가 유효한지 확인해주세요.")
            return []
        except Exception as e:
            st.error(f"검색 중 오류: {str(e)}")
            return []

    def get_law_detail(self, oc_code: str, law_id: str, law_name: str):
        """
        법령 상세 정보 수집 (정교한 필터링 적용)
        """
        params = {
            'OC': oc_code,
            'target': 'law',
            'ID': law_id,
            'type': 'XML'
        }
        try:
            response = requests.get(
                self.law_detail_url,
                params=params,
                timeout=15,
                verify=False
            )
            response.encoding = 'utf-8'
            if response.status_code != 200:
                st.warning(f"{law_name} 상세 정보 API 호출 실패 (HTTP {response.status_code})")
                return self._get_basic_info(law_id, law_name)

            content = response.text
            if content.startswith('\ufeff'):
                content = content[1:]

            root = ET.fromstring(content.encode('utf-8'))

            basic_info = root.find('기본정보')
            law_detail = {
                'law_id': law_id,
                'law_name': basic_info.findtext('법령명한글', law_name),
                'law_type': basic_info.findtext('법종구분', ''),
                'promulgation_date': basic_info.findtext('공포일자', ''),
                'enforcement_date': basic_info.findtext('시행일자', ''),
                'articles': [],
                'hierarchy': {'upper_laws': [], 'lower_laws': [], 'admin_rules': []}
            }

            articles_xml = root.findall('조문/조문단위')
            for article_elem in articles_xml:
                number = article_elem.findtext('조문번호', '')
                title = article_elem.findtext('조문제목', '').strip()
                content = self._get_element_text(article_elem.find('조문내용'))

                # [최종 수정] 1. 기본 유효성 검사 (번호와 내용이 있어야 함)
                if not number or not content:
                    continue

                # [최종 수정] 2. 내용이 '제O조'로 시작하는지 확인 (장/절 제목 필터링)
                #    - 조문번호에 '의2' 같은 것이 붙는 경우를 대비하여 유연하게 처리
                #    - 예: number='6의2' -> content는 '제6조의2'로 시작
                normalized_number = number.replace("의", "조의")
                if not content.startswith(f"제{normalized_number}"):
                    continue
                
                # [최종 수정] 3. 내용이 제목과 똑같으면 실질적 내용이 없는 것으로 간주 (빈 조문 필터링)
                #    - 예: 내용이 "제5조(자본금)" 이고, 제목이 "(자본금)"인 경우
                expected_title_only_content = f"제{number}조{title}"
                if content.strip() == expected_title_only_content.strip():
                    continue

                article_info = {
                    'number': number,
                    'title': title,
                    'content': content
                }
                law_detail['articles'].append(article_info)

            return law_detail

        except Exception as e:
            st.warning(f"{law_name} 수집 중 오류: {str(e)}")
            return self._get_basic_info(law_id, law_name)

    def _get_element_text(self, element):
        if element is None:
            return ""
        text = element.text or ""
        for child in element:
            text += self._get_element_text(child)
        if element.tail:
            text += element.tail
        return text.strip()

    def _get_basic_info(self, law_id: str, law_name: str):
        return {
            'law_id': law_id, 'law_name': law_name, 'law_type': '',
            'promulgation_date': '', 'enforcement_date': '', 'articles': [],
            'hierarchy': {'upper_laws': [], 'lower_laws': [], 'admin_rules': []}
        }

    def collect_law_hierarchy(self, law_id: str):
        hierarchy_url = f"https://www.law.go.kr/lsStmdTreePrint.do?lsiSeq={law_id}"
        hierarchy = {'upper_laws': [], 'lower_laws': [], 'admin_rules': []}
        try:
            response = requests.get(
                hierarchy_url, timeout=10, verify=False,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                upper_section = soup.find('h3', id='upperLawT')
                if upper_section and upper_section.find_next_sibling('ul'):
                    for link in upper_section.find_next_sibling('ul').find_all('a'):
                        hierarchy['upper_laws'].append(link.text.strip())
                lower_section = soup.find('h3', id='lowerLawT')
                if lower_section and lower_section.find_next_sibling('ul'):
                    for link in lower_section.find_next_sibling('ul').find_all('a'):
                        hierarchy['lower_laws'].append(link.text.strip())
                admin_section = soup.find('h3', id='admRuleT')
                if admin_section and admin_section.find_next_sibling('ul'):
                    for link in admin_section.find_next_sibling('ul').find_all('a'):
                        hierarchy['admin_rules'].append(link.text.strip())
        except Exception as e:
            st.warning(f"법령 체계도({law_id}) 수집 실패: {str(e)}")
        return hierarchy


def create_download_link(data, filename, file_type="json"):
    if file_type == "json":
        json_str = json.dumps(data, ensure_ascii=False, indent=2)
        b64 = base64.b64encode(json_str.encode()).decode()
        mime = "application/json"
    else:
        b64 = base64.b64encode(data.encode()).decode()
        mime = "text/markdown"
    href = f'<a href="data:{mime};base64,{b64}" download="{filename}">💾 {filename} 다운로드</a>'
    return href

def generate_markdown_report(collected_laws, collected_hierarchy, collected_precs):
    md_content = []
    md_content.append(f"# 법령 및 판례 수집 결과\n")
    md_content.append(f"수집 일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    total_articles = sum(len(law.get('articles', [])) for law in collected_laws.values())
    md_content.append(f"\n## 수집 요약\n")
    md_content.append(f"- 총 법령 수: {len(collected_laws)}개\n")
    md_content.append(f"- 총 법령 체계도: {len(collected_hierarchy)}개\n")
    md_content.append(f"- 총 유효 조문 수: {total_articles}개\n")
    md_content.append(f"\n## 법령 정보\n")

    for law_id, law in collected_laws.items():
        md_content.append(f"\n### {law['law_name']}\n")
        md_content.append(f"- 법령 ID: {law_id}\n")
        md_content.append(f"- 법종구분: {law.get('law_type', 'N/A')}\n")
        md_content.append(f"- 시행일자: {law.get('enforcement_date', 'N/A')}\n")

        if law_id in collected_hierarchy:
            hierarchy = collected_hierarchy[law_id]
            if any(hierarchy.values()):
                md_content.append(f"\n#### 법령 체계도\n")
                if hierarchy['upper_laws']: md_content.append(f"\n##### 상위법\n- " + "\n- ".join(hierarchy['upper_laws']))
                if hierarchy['lower_laws']: md_content.append(f"\n##### 하위법\n- " + "\n- ".join(hierarchy['lower_laws']))
                if hierarchy['admin_rules']: md_content.append(f"\n##### 행정규칙\n- " + "\n- ".join(hierarchy['admin_rules']))

        if law.get('articles'):
            md_content.append(f"\n#### 전체 조문 ({len(law['articles'])}개)\n")
            for article in law['articles']:
                # [최종 수정] 보고서 제목을 조문 번호와 제목으로 명확하게 구성
                title = article.get('title', '')
                number = article.get('number', '')
                header = f"제{number}조 {title}".strip()
                md_content.append(f"\n##### {header}\n")
                md_content.append(f"```{article['content']}```\n")
    
    return '\n'.join(md_content)

# 메인 UI
def main():
    st.title("📚 법제처 법령 수집기 (최종 수정 버전)")
    st.markdown("법제처 Open API를 활용하여 법령의 상세 정보와 조문을 안정적으로 수집합니다. **(최종 필터링 적용)**")

    with st.sidebar:
        st.header("⚙️ 설정")
        oc_code = st.text_input("기관코드 (OC)", placeholder="API 신청 시 발급받은 코드")
        law_name = st.text_input("법령명", placeholder="예: 민법, 여신전문금융업법")
        include_hierarchy = st.checkbox("법령 체계도 포함", value=True)
        c1, c2 = st.columns(2)
        search_btn = c1.button("🔍 검색", type="primary", use_container_width=True)
        collect_btn = c2.button("📥 수집", type="secondary", use_container_width=True)

    collector = LawCollectorStreamlit()

    if search_btn:
        if not oc_code or not law_name:
            st.error("기관코드와 법령명을 모두 입력해주세요!")
        else:
            with st.spinner(f"'{law_name}' 검색 중..."):
                st.session_state.search_results = collector.search_law(oc_code, law_name)
                if st.session_state.search_results:
                    st.success(f"{len(st.session_state.search_results)}개의 법령을 찾았습니다!")
                else:
                    st.warning("검색 결과가 없습니다.")
    
    if st.session_state.search_results:
        st.subheader("🔎 검색 결과")
        # UI 생략... (이전과 동일)
        selected_indices = []
        for i, law in enumerate(st.session_state.search_results):
            # ... UI 로직 ...
            if st.checkbox(f"{law['law_name']} ({law['law_type']}, 시행 {law['enforcement_date']})", key=f"select_{i}"):
                selected_indices.append(i)
        st.session_state.selected_laws = [st.session_state.search_results[i] for i in selected_indices]


    if collect_btn:
        if not oc_code or not st.session_state.selected_laws:
            st.error("기관코드를 입력하고 수집할 법령을 선택해주세요!")
        else:
            # 수집 로직 생략... (이전과 동일)
            progress_bar = st.progress(0, text="수집 대기 중...")
            st.session_state.collected_laws = {}
            st.session_state.collected_hierarchy = {}
            # ... 수집 로직 ...
            for law in st.session_state.selected_laws:
                law_detail = collector.get_law_detail(oc_code, law['law_id'], law['law_name'])
                if law_detail: st.session_state.collected_laws[law['law_id']] = law_detail
                if include_hierarchy:
                    hierarchy = collector.collect_law_hierarchy(law['law_id'])
                    if hierarchy: st.session_state.collected_hierarchy[law['law_id']] = hierarchy
                time.sleep(collector.delay)
            st.success("수집 완료!")

    if st.session_state.collected_laws:
        st.header("📊 수집 결과")
        # 결과 표시 탭 UI 생략... (이전과 동일)
        tab_names = ["📋 요약", "📖 법령 내용", "🌳 법령 체계도", "💾 다운로드"]
        tabs = st.tabs(tab_names)
        # ... 탭별 UI 로직 ...
        with tabs[3]: # 다운로드
            st.subheader("수집 결과 다운로드")
            json_data = {
                'collection_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'laws': st.session_state.collected_laws,
                'hierarchy': st.session_state.collected_hierarchy
            }
            st.markdown(create_download_link(json_data, f"law_{datetime.now():%Y%m%d}.json"), unsafe_allow_html=True)
            md_content = generate_markdown_report(st.session_state.collected_laws, st.session_state.collected_hierarchy, [])
            st.markdown(create_download_link(md_content, f"law_{datetime.now():%Y%m%d}.md", "md"), unsafe_allow_html=True)
            with st.expander("마크다운 미리보기"):
                st.markdown(md_content)

if __name__ == "__main__":
    main()
