"""
법제처 법령 수집기 - Streamlit 버전
GitHub/Streamlit Cloud에서 실행 가능한 웹 애플리케이션
"""

import streamlit as st
import requests
import xml.etree.ElementTree as ET
import json
import time
import re
import pandas as pd
from datetime import datetime
from bs4 import BeautifulSoup
import urllib3
from io import BytesIO
import base64

# SSL 경고 무시
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 페이지 설정
st.set_page_config(
    page_title="법제처 법령 수집기",
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

class LawCollectorStreamlit:
    """Streamlit용 법령 수집기"""
    
    def __init__(self):
        self.law_search_url = "http://www.law.go.kr/DRF/lawSearch.do"
        self.law_detail_url = "http://www.law.go.kr/DRF/lawService.do"
        self.prec_search_url = "http://www.law.go.kr/DRF/lawPrecSearch.do"
        self.delay = 0.5  # API 호출 간격
        
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
            # SSL 검증 비활성화
            response = requests.get(
                self.law_search_url, 
                params=params, 
                timeout=10,
                verify=False  # SSL 검증 비활성화
            )
            response.encoding = 'utf-8'
            
            if response.status_code != 200:
                st.error(f"API 응답 오류: HTTP {response.status_code}")
                return []
            
            content = response.text
            
            # HTML 체크
            if content.strip().startswith('<!DOCTYPE') or content.strip().startswith('<html'):
                st.error("API가 HTML을 반환했습니다.")
                return []
            
            # BOM 제거
            if content.startswith('\ufeff'):
                content = content[1:]
            
            # XML 파싱
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
            
        except Exception as e:
            st.error(f"검색 중 오류: {str(e)}")
            return []
    
    def get_law_detail(self, oc_code: str, law_id: str, law_name: str):
        """법령 상세 정보 수집"""
        # 웹 스크래핑으로 상세 정보 가져오기
        detail_url = f"https://www.law.go.kr/lsInfoP.do?lsiSeq={law_id}&efYd=99999999#0000"
        
        try:
            response = requests.get(
                detail_url,
                timeout=15,
                verify=False,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }
            )
            response.encoding = 'utf-8'
            
            if response.status_code != 200:
                st.warning(f"{law_name} 상세 정보 접근 실패")
                return self._get_basic_info(law_id, law_name)
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            law_detail = {
                'law_id': law_id,
                'law_name': law_name,
                'law_type': '',
                'promulgation_date': '',
                'enforcement_date': '',
                'articles': [],
                'supplementary_provisions': [],
                'tables': [],
                'hierarchy': {
                    'upper_laws': [],
                    'lower_laws': [],
                    'admin_rules': []
                }
            }
            
            # 법령 기본 정보 추출
            info_table = soup.find('table', {'class': 'tabletype'})
            if info_table:
                for row in info_table.find_all('tr'):
                    cells = row.find_all(['th', 'td'])
                    if len(cells) >= 2:
                        label = cells[0].text.strip()
                        value = cells[1].text.strip()
                        
                        if '법종구분' in label:
                            law_detail['law_type'] = value
                        elif '공포일자' in label:
                            law_detail['promulgation_date'] = value
                        elif '시행일자' in label:
                            law_detail['enforcement_date'] = value
            
            # iframe 내용 가져오기 시도
            iframe = soup.find('iframe', {'name': 'lawService'})
            if iframe:
                iframe_src = iframe.get('src', '')
                if iframe_src:
                    if not iframe_src.startswith('http'):
                        iframe_src = f"https://www.law.go.kr{iframe_src}"
                    
                    try:
                        iframe_response = requests.get(
                            iframe_src,
                            timeout=10,
                            verify=False,
                            headers={
                                'User-Agent': 'Mozilla/5.0'
                            }
                        )
                        iframe_soup = BeautifulSoup(iframe_response.text, 'html.parser')
                        
                        # iframe 내용에서 조문 추출
                        text = iframe_soup.get_text()
                        self._extract_articles_from_text(text, law_detail)
                        
                    except:
                        pass
            
            # 조문이 없으면 텍스트에서 추출 시도
            if not law_detail['articles']:
                page_text = soup.get_text()
                self._extract_articles_from_text(page_text, law_detail)
            
            return law_detail
            
        except Exception as e:
            st.warning(f"{law_name} 수집 중 오류: {str(e)}")
            return self._get_basic_info(law_id, law_name)
    
    def _extract_articles_from_text(self, text: str, law_detail: dict):
        """텍스트에서 조문 추출"""
        # 조문 패턴 매칭
        article_pattern = r'(제\d+조(?:의\d+)?)\s*(?:\((.*?)\))?\s*((?:(?!제\d+조)[\s\S]){1,2000})'
        matches = re.findall(article_pattern, text, re.MULTILINE)
        
        for match in matches[:200]:  # 최대 200개 조문
            if match[0]:
                article_info = {
                    'number': match[0],
                    'title': match[1] if match[1] else '',
                    'content': match[2].strip(),
                    'paragraphs': []
                }
                law_detail['articles'].append(article_info)
    
    def _get_basic_info(self, law_id: str, law_name: str):
        """기본 정보만 반환"""
        return {
            'law_id': law_id,
            'law_name': law_name,
            'law_type': '',
            'promulgation_date': '',
            'enforcement_date': '',
            'articles': [],
            'supplementary_provisions': [],
            'tables': [],
            'hierarchy': {
                'upper_laws': [],
                'lower_laws': [],
                'admin_rules': []
            }
        }
    
    def collect_law_hierarchy(self, law_id: str):
        """법령 체계도 수집"""
        hierarchy_url = f"https://www.law.go.kr/lsStmdTreePrint.do?lsiSeq={law_id}"
        
        hierarchy = {
            'upper_laws': [],
            'lower_laws': [],
            'admin_rules': []
        }
        
        try:
            response = requests.get(
                hierarchy_url,
                timeout=10,
                verify=False,
                headers={
                    'User-Agent': 'Mozilla/5.0'
                }
            )
            
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                
                # 법령 체계 정보 추출
                sections = soup.find_all(['div', 'ul'], class_=re.compile(r'stmd|tree'))
                
                for section in sections:
                    links = section.find_all('a')
                    for link in links:
                        law_text = link.text.strip()
                        if law_text:
                            # 카테고리 분류 (간단한 규칙)
                            if '시행령' in law_text or '시행규칙' in law_text:
                                hierarchy['lower_laws'].append(law_text)
                            elif '법률' in law_text and '시행' not in law_text:
                                hierarchy['upper_laws'].append(law_text)
                            else:
                                hierarchy['admin_rules'].append(law_text)
                
        except:
            pass
        
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
    md_content.append(f"- 총 판례 수: {len(collected_precs)}개\n")
    
    md_content.append(f"\n## 법령 정보\n")
    
    for law_id, law in collected_laws.items():
        md_content.append(f"\n### {law['law_name']}\n")
        md_content.append(f"- 법령 ID: {law_id}\n")
        md_content.append(f"- 법종구분: {law['law_type']}\n")
        md_content.append(f"- 시행일자: {law['enforcement_date']}\n")
        
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
            for article in law['articles'][:10]:
                md_content.append(f"\n##### {article['number']} {article['title']}\n")
                content = article['content'][:300] + '...' if len(article['content']) > 300 else article['content']
                md_content.append(f"{content}\n")
            
            if len(law['articles']) > 10:
                md_content.append(f"\n*... 외 {len(law['articles'])-10}개 조문*\n")
    
    return '\n'.join(md_content)

# 메인 UI
def main():
    st.title("📚 법제처 법령 수집기")
    st.markdown("법제처 Open API를 활용한 법령 및 판례 수집 도구")
    
    # 사이드바
    with st.sidebar:
        st.header("⚙️ 설정")
        
        # 기관코드 입력
        oc_code = st.text_input(
            "기관코드 (OC)",
            placeholder="이메일 @ 앞부분",
            help="예: test@korea.kr → test"
        )
        
        # 법령명 입력
        law_name = st.text_input(
            "법령명",
            placeholder="예: 민법, 상법, 형법",
            help="검색할 법령명을 입력하세요"
        )
        
        # 옵션
        st.subheader("수집 옵션")
        include_related = st.checkbox("관련 법령 포함", value=True)
        include_hierarchy = st.checkbox("법령 체계도 포함", value=True)
        collect_precedents = st.checkbox("판례 수집", value=False)
        
        if collect_precedents:
            max_precedents = st.number_input(
                "최대 판례 수",
                min_value=10,
                max_value=500,
                value=50,
                step=10
            )
        
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
        
        # 선택 가능한 테이블로 표시
        df = pd.DataFrame(st.session_state.search_results)
        df['선택'] = False
        
        edited_df = st.data_editor(
            df[['선택', 'law_name', 'law_type', 'enforcement_date']],
            column_config={
                "선택": st.column_config.CheckboxColumn(
                    "선택",
                    help="수집할 법령을 선택하세요",
                    default=False,
                ),
                "law_name": "법령명",
                "law_type": "법종구분",
                "enforcement_date": "시행일자"
            },
            disabled=['law_name', 'law_type', 'enforcement_date'],
            hide_index=True,
            use_container_width=True
        )
        
        # 선택된 법령 목록
        selected_laws = df[edited_df['선택']].to_dict('records')
        
        if selected_laws:
            st.info(f"{len(selected_laws)}개 법령이 선택되었습니다.")
    
    # 수집 실행
    if collect_btn:
        if not oc_code:
            st.error("기관코드를 입력해주세요!")
        elif not selected_laws:
            st.error("수집할 법령을 선택해주세요!")
        else:
            # 진행 상황 표시
            progress_bar = st.progress(0)
            status_text = st.empty()
            
            # 초기화
            st.session_state.collected_laws = {}
            st.session_state.collected_hierarchy = {}
            st.session_state.collected_precs = []
            
            total_steps = len(selected_laws)
            if include_hierarchy:
                total_steps += len(selected_laws)
            
            current_step = 0
            
            # 법령 수집
            for law in selected_laws:
                current_step += 1
                progress = current_step / total_steps
                progress_bar.progress(progress)
                status_text.text(f"수집 중: {law['law_name']}...")
                
                # 법령 상세 정보 수집
                law_detail = collector.get_law_detail(
                    oc_code,
                    law['law_id'],
                    law['law_name']
                )
                
                if law_detail:
                    st.session_state.collected_laws[law['law_id']] = law_detail
                
                # 법령 체계도 수집
                if include_hierarchy:
                    current_step += 1
                    progress = current_step / total_steps
                    progress_bar.progress(progress)
                    status_text.text(f"체계도 수집 중: {law['law_name']}...")
                    
                    hierarchy = collector.collect_law_hierarchy(law['law_id'])
                    if hierarchy:
                        st.session_state.collected_hierarchy[law['law_id']] = hierarchy
                        law_detail['hierarchy'] = hierarchy
                
                # API 부하 방지
                time.sleep(collector.delay)
            
            progress_bar.progress(1.0)
            status_text.text("수집 완료!")
            st.success(f"총 {len(st.session_state.collected_laws)}개 법령 수집 완료!")
    
    # 수집 결과 표시
    if st.session_state.collected_laws:
        st.header("📊 수집 결과")
        
        # 탭 생성
        tab1, tab2, tab3, tab4 = st.tabs(["📋 요약", "📖 법령 내용", "🌳 법령 체계도", "💾 다운로드"])
        
        with tab1:
            # 수집 요약
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("수집된 법령", f"{len(st.session_state.collected_laws)}개")
            with col2:
                st.metric("법령 체계도", f"{len(st.session_state.collected_hierarchy)}개")
            with col3:
                total_articles = sum(len(law.get('articles', [])) for law in st.session_state.collected_laws.values())
                st.metric("총 조문 수", f"{total_articles}개")
            
            # 수집된 법령 목록
            st.subheader("수집된 법령 목록")
            for law_id, law in st.session_state.collected_laws.items():
                with st.expander(f"{law['law_name']} ({law['law_type']})"):
                    st.write(f"- 법령 ID: {law_id}")
                    st.write(f"- 시행일자: {law['enforcement_date']}")
                    st.write(f"- 조문 수: {len(law.get('articles', []))}개")
        
        with tab2:
            # 법령 내용 표시
            st.subheader("법령 내용")
            
            # 법령 선택
            law_names = [law['law_name'] for law in st.session_state.collected_laws.values()]
            selected_law_name = st.selectbox("법령 선택", law_names)
            
            # 선택된 법령의 상세 내용 표시
            for law_id, law in st.session_state.collected_laws.items():
                if law['law_name'] == selected_law_name:
                    # 기본 정보
                    st.write(f"**법종구분:** {law['law_type']}")
                    st.write(f"**공포일자:** {law['promulgation_date']}")
                    st.write(f"**시행일자:** {law['enforcement_date']}")
                    
                    # 조문 표시
                    if law.get('articles'):
                        st.subheader("조문")
                        
                        # 조문 검색
                        search_term = st.text_input("조문 검색", placeholder="예: 제1조, 계약")
                        
                        for article in law['articles']:
                            # 검색어 필터링
                            if search_term and search_term not in article['number'] and search_term not in article['content']:
                                continue
                            
                            with st.expander(f"{article['number']} {article['title']}"):
                                st.write(article['content'])
                    else:
                        st.info("조문 정보가 없습니다.")
                    break
        
        with tab3:
            # 법령 체계도 시각화
            st.subheader("법령 체계도")
            
            for law_id, law in st.session_state.collected_laws.items():
                if law_id in st.session_state.collected_hierarchy:
                    hierarchy = st.session_state.collected_hierarchy[law_id]
                    
                    with st.expander(f"{law['law_name']} 체계도"):
                        col1, col2, col3 = st.columns(3)
                        
                        with col1:
                            st.write("**상위법**")
                            for upper in hierarchy.get('upper_laws', [])[:10]:
                                st.write(f"- {upper}")
                        
                        with col2:
                            st.write("**하위법**")
                            for lower in hierarchy.get('lower_laws', [])[:10]:
                                st.write(f"- {lower}")
                        
                        with col3:
                            st.write("**행정규칙**")
                            for admin in hierarchy.get('admin_rules', [])[:10]:
                                st.write(f"- {admin}")
        
        with tab4:
            # 다운로드
            st.subheader("수집 결과 다운로드")
            
            # JSON 다운로드
            json_data = {
                'collection_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'laws': st.session_state.collected_laws,
                'hierarchy': st.session_state.collected_hierarchy,
                'precedents': st.session_state.collected_precs
            }
            
            # JSON 다운로드 링크
            json_filename = f"law_collection_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            st.markdown(
                create_download_link(json_data, json_filename, "json"),
                unsafe_allow_html=True
            )
            
            # Markdown 다운로드
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
            
            # 미리보기
            with st.expander("마크다운 미리보기"):
                st.markdown(md_content[:2000] + "..." if len(md_content) > 2000 else md_content)

if __name__ == "__main__":
    main()
