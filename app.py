"""
개선된 법령 수집기 - 체계도 법령 선택 기능 추가
사용자가 법령 체계도의 법령들을 개별 선택할 수 있도록 개선
"""

import streamlit as st
import requests
import xml.etree.ElementTree as ET
import json
import time
import re
from datetime import datetime
from io import BytesIO
import base64
import urllib3
import zipfile

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
if 'selected_laws' not in st.session_state:
    st.session_state.selected_laws = []
if 'hierarchy_laws' not in st.session_state:
    st.session_state.hierarchy_laws = []  # 체계도 법령 저장
if 'selected_hierarchy_laws' not in st.session_state:
    st.session_state.selected_hierarchy_laws = []  # 체계도에서 선택된 법령

class LawCollectorStreamlit:
    """Streamlit용 법령 수집기 - API 직접 호출 방식"""
    
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
            
            # BOM 제거
            if content.startswith('\ufeff'):
                content = content[1:]
            
            # XML 파싱
            root = ET.fromstring(content.encode('utf-8'))
            laws = []
            
            for law_elem in root.findall('.//law'):
                law_id = law_elem.findtext('법령ID', '')
                law_name_full = law_elem.findtext('법령명한글', '')
                law_msn = law_elem.findtext('법령일련번호', '')
                
                if law_id and law_name_full:
                    law_info = {
                        'law_id': law_id,
                        'law_msn': law_msn,
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
    
    def get_law_detail(self, oc_code: str, law_id: str, law_msn: str, law_name: str):
        """법령 상세 정보 수집"""
        params = {
            'OC': oc_code,
            'target': 'law',
            'type': 'XML',
            'MST': law_msn,
            'mobileYn': 'N'
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
                st.warning(f"{law_name} 상세 정보 접근 실패 (HTTP {response.status_code})")
                return self._get_basic_info(law_id, law_name)
            
            content = response.text
            
            # BOM 제거
            if content.startswith('\ufeff'):
                content = content[1:]
            
            # XML 파싱
            try:
                root = ET.fromstring(content.encode('utf-8'))
            except ET.ParseError as e:
                st.warning(f"{law_name} XML 파싱 오류: {str(e)}")
                return self._get_basic_info(law_id, law_name)
            
            # 법령 정보 추출
            law_detail = {
                'law_id': law_id,
                'law_msn': law_msn,
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
            
            # 기본 정보 추출
            basic_info = root.find('.//기본정보')
            if basic_info is not None:
                law_detail['law_type'] = basic_info.findtext('법종구분', '')
                law_detail['promulgation_date'] = basic_info.findtext('공포일자', '')
                law_detail['enforcement_date'] = basic_info.findtext('시행일자', '')
            
            # 조문 추출
            articles_section = root.find('.//조문')
            if articles_section is not None:
                for article_unit in articles_section.findall('.//조문단위'):
                    article_info = self._extract_article_from_xml(article_unit)
                    if article_info:
                        law_detail['articles'].append(article_info)
            
            # 부칙 추출
            addendums = root.findall('.//부칙')
            for addendum in addendums:
                addendum_info = {
                    'number': addendum.findtext('부칙번호', ''),
                    'promulgation_date': addendum.findtext('부칙공포일자', ''),
                    'content': self._extract_text_from_element(addendum)
                }
                law_detail['supplementary_provisions'].append(addendum_info)
            
            return law_detail
            
        except Exception as e:
            st.warning(f"{law_name} 수집 중 오류: {str(e)}")
            return self._get_basic_info(law_id, law_name)
    
    def collect_law_hierarchy_improved(self, law_id: str, law_msn: str, oc_code: str, law_name: str):
        """법령 체계도 수집 - API 기반 개선된 방식"""
        hierarchy = {
            'upper_laws': [],
            'lower_laws': [],  
            'admin_rules': [],
            'related_laws': [],
        }
        
        try:
            # 현재 법령명에서 기본 법령명 추출
            base_name = law_name
            
            # 법령 타입 판별
            is_enforcement_decree = '시행령' in law_name
            is_enforcement_rule = '시행규칙' in law_name
            is_admin_rule = any(k in law_name for k in ['고시', '훈령', '예규', '지침'])
            
            # 기본 법령명 추출 (접미사 제거)
            for suffix in ['시행령', '시행규칙', '고시', '훈령', '예규', '지침']:
                base_name = base_name.replace(suffix, '').strip()
            
            with st.spinner(f"🔍 '{base_name}' 관련 법령 체계 검색 중..."):
                
                # 1. 상위법 검색
                if is_enforcement_decree or is_enforcement_rule or is_admin_rule:
                    # 기본 법률 검색
                    results = self.search_law(oc_code, base_name)
                    for result in results:
                        if (result['law_name'] == base_name or 
                            (base_name in result['law_name'] and '법' in result['law_name'] 
                             and not any(s in result['law_name'] for s in ['시행령', '시행규칙']))):
                            hierarchy['upper_laws'].append(result)
                            break
                    
                    # 시행규칙인 경우 시행령도 상위법
                    if is_enforcement_rule:
                        decree_name = f"{base_name} 시행령"
                        results = self.search_law(oc_code, decree_name)
                        for result in results[:1]:
                            if '시행령' in result['law_name']:
                                hierarchy['upper_laws'].append(result)
                
                # 2. 하위법령 검색
                if not is_enforcement_rule and not is_admin_rule:
                    # 시행령 검색
                    if not is_enforcement_decree:
                        decree_name = f"{base_name} 시행령"
                        results = self.search_law(oc_code, decree_name)
                        for result in results[:2]:
                            if '시행령' in result['law_name'] and base_name in result['law_name']:
                                hierarchy['lower_laws'].append(result)
                    
                    # 시행규칙 검색
                    rule_name = f"{base_name} 시행규칙"
                    results = self.search_law(oc_code, rule_name)
                    for result in results[:2]:
                        if '시행규칙' in result['law_name'] and base_name in result['law_name']:
                            hierarchy['lower_laws'].append(result)
                
                # 3. 행정규칙 검색
                if not is_admin_rule:
                    admin_types = ['고시', '훈령', '예규', '지침', '규정']
                    
                    for admin_type in admin_types:
                        search_patterns = [
                            f"{base_name} {admin_type}",
                            f"{base_name}{admin_type}",
                        ]
                        
                        for pattern in search_patterns:
                            results = self.search_law(oc_code, pattern)
                            
                            for result in results[:3]:
                                if not any(r['law_id'] == result['law_id'] for r in hierarchy['admin_rules']):
                                    if admin_type in result['law_name'] and base_name in result['law_name']:
                                        hierarchy['admin_rules'].append(result)
                            
                            if len(hierarchy['admin_rules']) >= 10:
                                break
                        
                        time.sleep(self.delay)
                
                # 4. 관련 법령 검색
                related_keywords = ['특별법', '기본법', '특례법']
                
                if len(hierarchy['related_laws']) < 5:
                    for keyword in related_keywords:
                        if keyword not in base_name:
                            search_term = base_name.replace('법', '') + keyword
                            results = self.search_law(oc_code, search_term)
                            
                            for result in results[:1]:
                                if result['law_id'] != law_id:
                                    hierarchy['related_laws'].append(result)
                
        except Exception as e:
            st.error(f"법령 체계도 수집 중 오류: {str(e)}")
        
        return hierarchy
    
    def _extract_article_from_xml(self, article_elem):
        """XML 요소에서 조문 정보 추출"""
        article_info = {
            'number': '',
            'title': '',
            'content': '',
            'paragraphs': []
        }
        
        article_num = article_elem.findtext('조문번호', '')
        if article_num:
            article_info['number'] = f"제{article_num}조"
        
        article_info['title'] = article_elem.findtext('조문제목', '')
        
        article_content = article_elem.findtext('조문내용', '')
        if not article_content:
            article_content = self._extract_text_from_element(article_elem)
        
        article_info['content'] = article_content
        
        for para_elem in article_elem.findall('.//항'):
            para_num = para_elem.findtext('항번호', '')
            para_content = para_elem.findtext('항내용', '')
            if para_num and para_content:
                article_info['paragraphs'].append({
                    'number': para_num,
                    'content': para_content
                })
        
        return article_info if article_info['number'] else None
    
    def _extract_text_from_element(self, elem):
        """XML 요소에서 텍스트 추출"""
        texts = []
        for text in elem.itertext():
            if text and text.strip():
                texts.append(text.strip())
        return ' '.join(texts)
    
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
    
    def export_laws_to_zip(self, laws_dict: dict) -> bytes:
        """선택된 법령들을 ZIP 파일로 압축"""
        zip_buffer = BytesIO()
        
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            # 전체 JSON 데이터
            all_data = {
                'collection_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'total_laws': len(laws_dict),
                'laws': laws_dict
            }
            
            zip_file.writestr(
                'all_laws.json',
                json.dumps(all_data, ensure_ascii=False, indent=2)
            )
            
            # 개별 법령 파일
            for law_id, law in laws_dict.items():
                safe_name = re.sub(r'[\\/*?:"<>|]', '_', law['law_name'])
                
                # JSON 파일
                zip_file.writestr(
                    f'laws/{safe_name}.json',
                    json.dumps(law, ensure_ascii=False, indent=2)
                )
                
                # 텍스트 파일
                text_content = self._format_law_as_text(law)
                zip_file.writestr(
                    f'laws/{safe_name}.txt',
                    text_content
                )
            
            # README 파일
            readme_content = self._create_readme(laws_dict)
            zip_file.writestr('README.md', readme_content)
        
        zip_buffer.seek(0)
        return zip_buffer.getvalue()
    
    def _format_law_as_text(self, law: dict) -> str:
        """법령을 텍스트 형식으로 변환"""
        lines = []
        
        lines.append(f"{'=' * 60}")
        lines.append(f"{law['law_name']}")
        lines.append(f"{'=' * 60}")
        lines.append(f"법종구분: {law.get('law_type', '')}")
        lines.append(f"공포일자: {law.get('promulgation_date', '')}")
        lines.append(f"시행일자: {law.get('enforcement_date', '')}")
        lines.append(f"{'=' * 60}\n")
        
        if law.get('articles'):
            lines.append("【조문】\n")
            for article in law['articles']:
                lines.append(f"\n{article['number']} {article['title']}")
                lines.append(f"{article['content']}\n")
                
                if article.get('paragraphs'):
                    for para in article['paragraphs']:
                        lines.append(f"  ② {para['content']}")
        
        if law.get('supplementary_provisions'):
            lines.append("\n\n【부칙】\n")
            for supp in law['supplementary_provisions']:
                lines.append(f"\n부칙 <{supp['promulgation_date']}>")
                lines.append(supp['content'])
        
        return '\n'.join(lines)
    
    def _create_readme(self, laws_dict: dict) -> str:
        """README 파일 생성"""
        content = f"""# 법령 수집 결과

수집 일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
총 법령 수: {len(laws_dict)}개

## 📁 파일 구조

- `all_laws.json`: 전체 법령 데이터 (JSON)
- `laws/`: 개별 법령 파일 디렉토리
  - `*.json`: 법령별 상세 데이터
  - `*.txt`: 법령별 텍스트 형식
- `README.md`: 이 파일

## 📊 수집된 법령 목록

"""
        for law_id, law in laws_dict.items():
            content += f"- {law['law_name']} ({law['law_type']})\n"
        
        return content


# 메인 UI
def main():
    st.title("📚 법제처 법령 수집기")
    st.markdown("법제처 Open API를 활용한 법령 수집 도구")
    
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
        
        # 버튼
        col1, col2 = st.columns(2)
        with col1:
            search_btn = st.button("🔍 검색", type="primary", use_container_width=True)
        with col2:
            reset_btn = st.button("🔄 초기화", type="secondary", use_container_width=True)
            
        if reset_btn:
            # 세션 상태 초기화
            st.session_state.search_results = []
            st.session_state.selected_laws = []
            st.session_state.hierarchy_laws = []
            st.session_state.selected_hierarchy_laws = []
            st.session_state.collected_laws = {}
            st.experimental_rerun()
    
    # 메인 컨텐츠
    collector = LawCollectorStreamlit()
    
    # STEP 1: 법령 검색
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
    
    # STEP 2: 검색 결과에서 법령 선택
    if st.session_state.search_results:
        st.header("📋 STEP 1: 법령 선택")
        st.info("체계도를 확인할 법령을 선택하세요")
        
        # 전체 선택/해제
        col1, col2 = st.columns([3, 1])
        with col2:
            select_all = st.checkbox("전체 선택")
        
        # 테이블 헤더
        col1, col2, col3, col4 = st.columns([1, 3, 2, 2])
        with col1:
            st.markdown("**선택**")
        with col2:
            st.markdown("**법령명**")
        with col3:
            st.markdown("**법종구분**")
        with col4:
            st.markdown("**시행일자**")
        
        st.divider()
        
        # 선택된 법령 추적
        selected_indices = []
        
        # 각 법령에 대한 체크박스
        for i, law in enumerate(st.session_state.search_results):
            col1, col2, col3, col4 = st.columns([1, 3, 2, 2])
            
            with col1:
                is_selected = st.checkbox("", key=f"select_{i}", value=select_all)
                if is_selected:
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
            st.success(f"{len(st.session_state.selected_laws)}개 법령이 선택되었습니다.")
            
            # 체계도 검색 버튼
            if st.button("🌳 법령 체계도 검색", type="primary", use_container_width=True):
                # 체계도 수집
                all_hierarchy_laws = []
                
                progress_bar = st.progress(0)
                status_text = st.empty()
                
                for idx, law in enumerate(st.session_state.selected_laws):
                    progress = (idx + 1) / len(st.session_state.selected_laws)
                    progress_bar.progress(progress)
                    status_text.text(f"체계도 검색 중: {law['law_name']}...")
                    
                    # 체계도 수집
                    hierarchy = collector.collect_law_hierarchy_improved(
                        law['law_id'],
                        law.get('law_msn', ''),
                        oc_code,
                        law['law_name']
                    )
                    
                    # 체계도의 모든 법령을 하나의 리스트로 모음
                    for category in ['upper_laws', 'lower_laws', 'admin_rules', 'related_laws']:
                        for h_law in hierarchy.get(category, []):
                            # 중복 제거
                            if not any(l['law_id'] == h_law['law_id'] for l in all_hierarchy_laws):
                                h_law['main_law'] = law['law_name']  # 어느 법령의 체계도인지 표시
                                h_law['category'] = category  # 카테고리 정보 추가
                                all_hierarchy_laws.append(h_law)
                    
                    # 주 법령도 추가
                    law['main_law'] = law['law_name']
                    law['category'] = 'main'
                    all_hierarchy_laws.append(law)
                    
                    time.sleep(collector.delay)
                
                progress_bar.progress(1.0)
                status_text.text("체계도 검색 완료!")
                
                st.session_state.hierarchy_laws = all_hierarchy_laws
    
    # STEP 3: 체계도 법령 선택
    if st.session_state.hierarchy_laws:
        st.header("🌳 STEP 2: 체계도 법령 선택")
        st.info("수집할 법령을 선택하세요")
        
        # 카테고리별 분류
        categories = {
            'main': '주 법령',
            'upper_laws': '상위법',
            'lower_laws': '하위법령',
            'admin_rules': '행정규칙',
            'related_laws': '관련법령'
        }
        
        # 카테고리별 탭
        tabs = st.tabs(list(categories.values()))
        
        selected_hierarchy_indices = []
        
        for tab_idx, (category_key, category_name) in enumerate(categories.items()):
            with tabs[tab_idx]:
                # 해당 카테고리의 법령들
                category_laws = [
                    (idx, law) for idx, law in enumerate(st.session_state.hierarchy_laws)
                    if law.get('category') == category_key
                ]
                
                if category_laws:
                    # 전체 선택
                    select_all_cat = st.checkbox(f"전체 선택", key=f"select_all_{category_key}")
                    
                    # 테이블 헤더
                    col1, col2, col3, col4, col5 = st.columns([1, 3, 2, 2, 2])
                    with col1:
                        st.markdown("**선택**")
                    with col2:
                        st.markdown("**법령명**")
                    with col3:
                        st.markdown("**법종구분**")
                    with col4:
                        st.markdown("**시행일자**")
                    with col5:
                        st.markdown("**관련 주 법령**")
                    
                    st.divider()
                    
                    # 각 법령 표시
                    for idx, law in category_laws:
                        col1, col2, col3, col4, col5 = st.columns([1, 3, 2, 2, 2])
                        
                        with col1:
                            is_selected = st.checkbox(
                                "", 
                                key=f"h_select_{idx}", 
                                value=select_all_cat
                            )
                            if is_selected:
                                selected_hierarchy_indices.append(idx)
                        
                        with col2:
                            st.write(law['law_name'])
                        
                        with col3:
                            st.write(law.get('law_type', ''))
                        
                        with col4:
                            st.write(law.get('enforcement_date', ''))
                        
                        with col5:
                            st.write(law.get('main_law', ''))
                else:
                    st.info(f"{category_name}이 없습니다.")
        
        # 선택된 법령 저장
        st.session_state.selected_hierarchy_laws = [
            st.session_state.hierarchy_laws[i] for i in set(selected_hierarchy_indices)
        ]
        
        if st.session_state.selected_hierarchy_laws:
            st.success(f"총 {len(st.session_state.selected_hierarchy_laws)}개 법령이 선택되었습니다.")
            
            # 수집 및 다운로드 버튼
            col1, col2 = st.columns(2)
            with col1:
                collect_btn = st.button("📥 선택한 법령 수집", type="primary", use_container_width=True)
            with col2:
                if st.session_state.collected_laws:
                    download_btn = st.button("💾 다운로드", type="secondary", use_container_width=True)
    
    # STEP 4: 법령 수집
    if 'collect_btn' in locals() and collect_btn:
        if st.session_state.selected_hierarchy_laws:
            progress_bar = st.progress(0)
            status_text = st.empty()
            
            # 수집 시작
            collected_laws = {}
            total = len(st.session_state.selected_hierarchy_laws)
            
            for idx, law in enumerate(st.session_state.selected_hierarchy_laws):
                progress = (idx + 1) / total
                progress_bar.progress(progress)
                status_text.text(f"수집 중 ({idx + 1}/{total}): {law['law_name']}...")
                
                # 법령 상세 정보 수집
                law_detail = collector.get_law_detail(
                    oc_code,
                    law['law_id'],
                    law.get('law_msn', ''),
                    law['law_name']
                )
                
                if law_detail:
                    collected_laws[law['law_id']] = law_detail
                
                time.sleep(collector.delay)
            
            progress_bar.progress(1.0)
            status_text.text("수집 완료!")
            
            st.session_state.collected_laws = collected_laws
            st.success(f"✅ {len(collected_laws)}개 법령 수집 완료!")
    
    # STEP 5: 다운로드
    if st.session_state.collected_laws:
        st.header("💾 STEP 3: 다운로드")
        
        col1, col2, col3 = st.columns(3)
        
        with col1:
            # JSON 다운로드
            json_data = {
                'collection_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'laws': st.session_state.collected_laws
            }
            json_str = json.dumps(json_data, ensure_ascii=False, indent=2)
            
            st.download_button(
                label="📄 JSON 다운로드",
                data=json_str,
                file_name=f"laws_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                mime="application/json",
                use_container_width=True
            )
        
        with col2:
            # ZIP 다운로드
            zip_data = collector.export_laws_to_zip(st.session_state.collected_laws)
            
            st.download_button(
                label="📦 ZIP 다운로드",
                data=zip_data,
                file_name=f"laws_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
                mime="application/zip",
                use_container_width=True
            )
        
        with col3:
            # 수집 결과 요약
            st.metric("수집된 법령", f"{len(st.session_state.collected_laws)}개")
        
        # 수집된 법령 목록 표시
        with st.expander("📊 수집된 법령 목록"):
            for law_id, law in st.session_state.collected_laws.items():
                st.write(f"- {law['law_name']} ({law['law_type']})")


if __name__ == "__main__":
    main()
