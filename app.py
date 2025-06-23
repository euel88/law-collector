"""
법제처 법령 수집기 - Streamlit 버전 (API 기반 수정)
GitHub/Streamlit Cloud에서 실행 가능한 웹 애플리케이션
- 웹 스크래핑 방식에서 공식 Open API 호출 방식으로 변경하여 안정성 확보
- pandas 의존성 완전 제거 버전
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
    page_title="법제처 법령 수집기 (API 수정 버전)",
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
        """법령 검색 (기존과 동일)"""
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
        법령 상세 정보 수집 (API 직접 호출 방식으로 수정)
        - 웹 스크래핑 대신 lawService.do API를 사용하여 조문 정보까지 모두 가져옵니다.
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

            # 기본 정보 추출
            basic_info = root.find('기본정보')
            law_detail = {
                'law_id': law_id,
                'law_name': basic_info.findtext('법령명한글', law_name),
                'law_type': basic_info.findtext('법종구분', ''),
                'promulgation_date': basic_info.findtext('공포일자', ''),
                'enforcement_date': basic_info.findtext('시행일자', ''),
                'articles': [],
                'supplementary_provisions': [],
                'tables': [],
                'hierarchy': {
                    'upper_laws': [], 'lower_laws': [], 'admin_rules': []
                }
            }

            # 조문 정보 추출
            articles_xml = root.findall('조문/조문단위')
            for article_elem in articles_xml:
                article_info = {
                    'number': article_elem.findtext('조문번호', ''),
                    'title': article_elem.findtext('조문제목', '').strip(),
                    'content': self._get_element_text(article_elem.find('조문내용')),
                    'paragraphs': [] # 상세 항/호 파싱은 필요시 추가 구현
                }
                if article_info['number'] or article_info['content']:
                     law_detail['articles'].append(article_info)


            return law_detail

        except Exception as e:
            st.warning(f"{law_name} 수집 중 오류: {str(e)}")
            # 실패 시에도 기본 구조는 반환
            return self._get_basic_info(law_id, law_name)
            
    def _get_element_text(self, element):
        """XML Element의 모든 텍스트를 재귀적으로 추출"""
        if element is None:
            return ""
        text = element.text or ""
        for child in element:
            text += self._get_element_text(child)
        if element.tail:
            text += element.tail
        return text.strip()

    def _get_basic_info(self, law_id: str, law_name: str):
        """기본 정보만 반환 (오류 발생 시 사용)"""
        return {
            'law_id': law_id, 'law_name': law_name, 'law_type': '',
            'promulgation_date': '', 'enforcement_date': '',
            'articles': [], 'supplementary_provisions': [], 'tables': [],
            'hierarchy': {'upper_laws': [], 'lower_laws': [], 'admin_rules': []}
        }

    def collect_law_hierarchy(self, law_id: str):
        """법령 체계도 수집 (기존 스크래핑 방식 유지)"""
        hierarchy_url = f"https://www.law.go.kr/lsStmdTreePrint.do?lsiSeq={law_id}"
        hierarchy = {'upper_laws': [], 'lower_laws': [], 'admin_rules': []}
        try:
            response = requests.get(
                hierarchy_url, timeout=10, verify=False,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                
                # 상위법령 (h3#upperLawT)
                upper_section = soup.find('h3', id='upperLawT')
                if upper_section and upper_section.find_next_sibling('ul'):
                    for link in upper_section.find_next_sibling('ul').find_all('a'):
                        hierarchy['upper_laws'].append(link.text.strip())

                # 하위법령 (h3#lowerLawT)
                lower_section = soup.find('h3', id='lowerLawT')
                if lower_section and lower_section.find_next_sibling('ul'):
                    for link in lower_section.find_next_sibling('ul').find_all('a'):
                        hierarchy['lower_laws'].append(link.text.strip())

                # 행정규칙 (h3#admRuleT)
                admin_section = soup.find('h3', id='admRuleT')
                if admin_section and admin_section.find_next_sibling('ul'):
                    for link in admin_section.find_next_sibling('ul').find_all('a'):
                        hierarchy['admin_rules'].append(link.text.strip())
        except Exception as e:
            st.warning(f"법령 체계도({law_id}) 수집 실패: {str(e)}")
            pass # 실패해도 전체 프로세스에 영향 없도록
        return hierarchy


def create_download_link(data, filename, file_type="json"):
    """다운로드 링크 생성"""
    if file_type == "json":
        json_str = json.dumps(data, ensure_ascii=False, indent=2)
        b64 = base64.b64encode(json_str.encode()).decode()
        mime = "application/json"
    else:  # markdown
        b64 = base64.b64encode(data.encode()).decode()
        mime = "text/markdown"
    
    href = f'<a href="data:{mime};base64,{b64}" download="{filename}">💾 {filename} 다운로드</a>'
    return href

def generate_markdown_report(collected_laws, collected_hierarchy, collected_precs):
    """마크다운 보고서 생성"""
    md_content = []
    md_content.append(f"# 법령 및 판례 수집 결과\n")
    md_content.append(f"수집 일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    
    md_content.append(f"\n## 수집 요약\n")
    md_content.append(f"- 총 법령 수: {len(collected_laws)}개\n")
    md_content.append(f"- 총 법령 체계도: {len(collected_hierarchy)}개\n")
    total_articles = sum(len(law.get('articles', [])) for law in collected_laws.values())
    md_content.append(f"- 총 조문 수: {total_articles}개\n")
    
    md_content.append(f"\n## 법령 정보\n")
    
    for law_id, law in collected_laws.items():
        md_content.append(f"\n### {law['law_name']}\n")
        md_content.append(f"- 법령 ID: {law_id}\n")
        md_content.append(f"- 법종구분: {law.get('law_type', 'N/A')}\n")
        md_content.append(f"- 시행일자: {law.get('enforcement_date', 'N/A')}\n")
        
        # 법령 체계도
        if law_id in collected_hierarchy:
            hierarchy = collected_hierarchy[law_id]
            if any([hierarchy['upper_laws'], hierarchy['lower_laws'], hierarchy['admin_rules']]):
                md_content.append(f"\n#### 법령 체계도\n")
                
                if hierarchy['upper_laws']:
                    md_content.append(f"\n##### 상위법\n")
                    for law_name in hierarchy['upper_laws'][:5]:
                        md_content.append(f"- {law_name}\n")
                
                if hierarchy['lower_laws']:
                    md_content.append(f"\n##### 하위법\n")
                    for law_name in hierarchy['lower_laws'][:5]:
                        md_content.append(f"- {law_name}\n")
                
                if hierarchy['admin_rules']:
                    md_content.append(f"\n##### 행정규칙\n")
                    for law_name in hierarchy['admin_rules'][:5]:
                        md_content.append(f"- {law_name}\n")
        
        # 조문
        if law.get('articles'):
            md_content.append(f"\n#### 주요 조문\n")
            for article in law['articles'][:10]: # 보고서에는 10개만 포함
                title = article.get('title', '')
                number = article.get('number', '')
                header = f"{number} {f'({title})' if title else ''}".strip()
                md_content.append(f"\n##### {header}\n")
                content = article['content'][:300] + '...' if len(article['content']) > 300 else article['content']
                md_content.append(f"```{content}```\n")
            
            if len(law['articles']) > 10:
                md_content.append(f"\n*... 외 {len(law['articles'])-10}개 조문*\n")
    
    return '\n'.join(md_content)

# 메인 UI
def main():
    st.title("📚 법제처 법령 수집기 (API 수정 버전)")
    st.markdown("법제처 Open API를 활용하여 법령의 상세 정보와 조문을 안정적으로 수집합니다.")
    
    # 사이드바
    with st.sidebar:
        st.header("⚙️ 설정")
        
        # 기관코드 입력
        oc_code = st.text_input(
            "기관코드 (OC)",
            placeholder="API 신청 시 발급받은 코드",
            help="법제처 Open API를 신청하고 발급받은 인증키를 입력하세요."
        )
        
        # 법령명 입력
        law_name = st.text_input(
            "법령명",
            placeholder="예: 민법, 상법, 형법",
            help="검색할 법령명을 입력하세요"
        )
        
        # 옵션
        st.subheader("수집 옵션")
        include_hierarchy = st.checkbox("법령 체계도 포함", value=True)
        # 판례 수집 기능은 현재 구현에서 제외
        # collect_precedents = st.checkbox("판례 수집", value=False)
        
        # 버튼
        col1, col2 = st.columns(2)
        with col1:
            search_btn = st.button("🔍 검색", type="primary", use_container_width=True)
        with col2:
            collect_btn = st.button("📥 수집", type="secondary", use_container_width=True)

    # 메인 컨텐츠
    collector = LawCollectorStreamlit()
    
    # 검색 실행
    if search_btn:
        if not oc_code:
            st.error("기관코드를 입력해주세요!")
        elif not law_name:
            st.error("법령명을 입력해주세요!")
        else:
            with st.spinner(f"'{law_name}' 검색 중..."):
                results = collector.search_law(oc_code, law_name)
                
                if results:
                    st.success(f"{len(results)}개의 법령을 찾았습니다!")
                    st.session_state.search_results = results
                else:
                    st.warning("검색 결과가 없습니다.")
                    st.session_state.search_results = []
    
    # 검색 결과 표시
    if st.session_state.search_results:
        st.subheader("🔎 검색 결과")
        
        # 테이블 헤더
        col1, col2, col3, col4 = st.columns([1, 4, 2, 2])
        col1.markdown("**선택**")
        col2.markdown("**법령명**")
        col3.markdown("**법종구분**")
        col4.markdown("**시행일자**")
        st.divider()
        
        # 선택된 법령 추적
        selected_indices = []
        
        # 각 법령에 대한 체크박스와 정보 표시
        for i, law in enumerate(st.session_state.search_results):
            col1, col2, col3, col4 = st.columns([1, 4, 2, 2])
            
            with col1:
                if st.checkbox("", key=f"select_{i}"):
                    selected_indices.append(i)
            with col2:
                st.write(law['law_name'])
            with col3:
                st.write(law['law_type'])
            with col4:
                st.write(law['enforcement_date'])
        
        # 선택된 법령 저장
        st.session_state.selected_laws = [
            st.session_state.search_results[i] for i in selected_indices
        ]
        
        if st.session_state.selected_laws:
            st.info(f"{len(st.session_state.selected_laws)}개 법령이 선택되었습니다.")

    # 수집 실행
    if collect_btn:
        if not oc_code:
            st.error("기관코드를 입력해주세요!")
        elif not st.session_state.selected_laws:
            st.error("수집할 법령을 선택해주세요!")
        else:
            total_tasks = len(st.session_state.selected_laws) * (2 if include_hierarchy else 1)
            progress_bar = st.progress(0, text="수집 대기 중...")
            
            # 초기화
            st.session_state.collected_laws = {}
            st.session_state.collected_hierarchy = {}
            st.session_state.collected_precs = []
            
            current_task = 0
            for law in st.session_state.selected_laws:
                # 1. 법령 상세 정보 수집
                current_task += 1
                progress_text = f"수집 중 ({current_task}/{total_tasks}): {law['law_name']}..."
                progress_bar.progress(current_task / total_tasks, text=progress_text)
                law_detail = collector.get_law_detail(oc_code, law['law_id'], law['law_name'])
                if law_detail:
                    st.session_state.collected_laws[law['law_id']] = law_detail
                
                # 2. 법령 체계도 수집
                if include_hierarchy:
                    current_task += 1
                    progress_text = f"체계도 수집 중 ({current_task}/{total_tasks}): {law['law_name']}..."
                    progress_bar.progress(current_task / total_tasks, text=progress_text)
                    hierarchy = collector.collect_law_hierarchy(law['law_id'])
                    if hierarchy:
                        st.session_state.collected_hierarchy[law['law_id']] = hierarchy
                        if law_detail:
                            law_detail['hierarchy'] = hierarchy
                
                time.sleep(collector.delay) # API 부하 방지
            
            progress_bar.progress(1.0, text="수집 완료!")
            st.success(f"총 {len(st.session_state.collected_laws)}개 법령 수집 완료!")

    # 수집 결과 표시
    if st.session_state.collected_laws:
        st.header("📊 수집 결과")
        
        tab_names = ["📋 요약", "📖 법령 내용"]
        if st.session_state.collected_hierarchy:
            tab_names.append("🌳 법령 체계도")
        tab_names.append("💾 다운로드")
        
        tabs = st.tabs(tab_names)
        
        # 요약 탭
        with tabs[0]:
            col1, col2, col3 = st.columns(3)
            total_articles = sum(len(law.get('articles', [])) for law in st.session_state.collected_laws.values())
            col1.metric("수집된 법령", f"{len(st.session_state.collected_laws)}개")
            col2.metric("법령 체계도", f"{len(st.session_state.collected_hierarchy)}개")
            col3.metric("총 조문 수", f"{total_articles}개")
            
            st.subheader("수집된 법령 목록")
            for law_id, law in st.session_state.collected_laws.items():
                with st.expander(f"{law['law_name']} ({law.get('law_type', 'N/A')})"):
                    st.write(f"- 법령 ID: {law_id}")
                    st.write(f"- 시행일자: {law.get('enforcement_date', 'N/A')}")
                    st.write(f"- 수집된 조문 수: {len(law.get('articles', []))}개")
        
        # 법령 내용 탭
        with tabs[1]:
            st.subheader("법령 내용")
            law_names = [law['law_name'] for law in st.session_state.collected_laws.values()]
            if not law_names:
                st.warning("표시할 법령이 없습니다.")
            else:
                selected_law_name = st.selectbox("법령 선택", law_names)
                
                for law_id, law in st.session_state.collected_laws.items():
                    if law['law_name'] == selected_law_name:
                        st.write(f"**법종구분:** {law.get('law_type', 'N/A')}")
                        st.write(f"**시행일자:** {law.get('enforcement_date', 'N/A')}")
                        
                        if law.get('articles'):
                            st.subheader(f"조문 ({len(law['articles'])}개)")
                            search_term = st.text_input("조문 검색", placeholder="예: 제1조, 계약, 손해배상", key=f"search_{law_id}")
                            
                            for article in law['articles']:
                                content = article['content']
                                title = article.get('title', '')
                                number = article.get('number', '')
                                
                                if search_term and search_term.lower() not in content.lower() and search_term not in number and search_term not in title:
                                    continue
                                
                                header = f"{number} {f'({title})' if title else ''}".strip()
                                with st.expander(header):
                                    st.write(content)
                        else:
                            st.info("수집된 조문 정보가 없습니다.")
                        break
        
        # 법령 체계도 탭 (존재할 경우)
        if st.session_state.collected_hierarchy:
            with tabs[2]:
                st.subheader("법령 체계도")
                for law_id, law in st.session_state.collected_laws.items():
                    if law_id in st.session_state.collected_hierarchy:
                        hierarchy = st.session_state.collected_hierarchy[law_id]
                        with st.expander(f"{law['law_name']} 체계도"):
                            col1, col2, col3 = st.columns(3)
                            col1.write("**상위법**")
                            col1.json(hierarchy.get('upper_laws', []), expanded=False)
                            col2.write("**하위법**")
                            col2.json(hierarchy.get('lower_laws', []), expanded=False)
                            col3.write("**행정규칙**")
                            col3.json(hierarchy.get('admin_rules', []), expanded=False)

        # 다운로드 탭
        with tabs[-1]:
            st.subheader("수집 결과 다운로드")
            
            # JSON 데이터
            json_data = {
                'collection_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'laws': st.session_state.collected_laws,
                'hierarchy': st.session_state.collected_hierarchy,
                'precedents': st.session_state.collected_precs
            }
            json_filename = f"law_collection_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            st.markdown(
                create_download_link(json_data, json_filename, "json"),
                unsafe_allow_html=True
            )
            
            # Markdown 보고서
            md_content = generate_markdown_report(
                st.session_state.collected_laws,
                st.session_state.collected_hierarchy,
                st.session_state.collected_precs
            )
            md_filename = f"law_collection_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
            st.markdown(
                create_download_link(md_content, md_filename, "markdown"),
                unsafe_allow_html=True
            )
            
            with st.expander("마크다운 미리보기"):
                st.markdown(md_content[:3000] + "\n..." if len(md_content) > 3000 else md_content)

if __name__ == "__main__":
    main()
