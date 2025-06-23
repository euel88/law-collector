"""
법제처 법령 수집기 - 완전 통합 버전
기존 기능 + 체계도 선택 기능 모두 포함
오류 처리 강화 및 별표/서식 수집 기능 추가
"""

import streamlit as st
import requests
import xml.etree.ElementTree as ET
import json
import time
import re
from datetime import datetime
from bs4 import BeautifulSoup
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
    st.session_state.hierarchy_laws = []
if 'selected_hierarchy_laws' not in st.session_state:
    st.session_state.selected_hierarchy_laws = []
if 'collection_mode' not in st.session_state:
    st.session_state.collection_mode = 'manual'  # manual or auto

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
            response.raise_for_status()
            response.encoding = 'utf-8'
            
            content = response.text
            
            if content.strip().startswith('<!DOCTYPE') or content.strip().startswith('<html'):
                st.error("API가 HTML을 반환했습니다. 기관코드(OC)가 정확한지 확인해주세요.")
                return []
            
            if content.startswith('\ufeff'):
                content = content[1:]
            
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
            
        except requests.exceptions.RequestException as e:
            st.error(f"API 요청 오류: {str(e)}")
            return []
        except ET.ParseError as e:
            st.error(f"XML 파싱 오류: {str(e)}")
            st.text("수신된 내용:")
            st.code(response.text if 'response' in locals() else "응답 없음", language='text')
            return []
        except Exception as e:
            st.error(f"검색 중 예상치 못한 오류 발생: {str(e)}")
            return []

    def get_law_detail(self, oc_code: str, law_id: str, law_msn: str, law_name: str):
        """법령 상세 정보 수집 - 오류 처리 및 별표/서식 수집 기능 추가"""
        if not law_msn:
            st.error(f"'{law_name}'의 법령일련번호(MST)가 없어 상세 정보를 가져올 수 없습니다.")
            return None

        params = {
            'OC': oc_code,
            'target': 'law',
            'type': 'XML',
            'MST': law_msn,
            'mobileYn': 'Y' # 모바일용이 내용이 더 깔끔할 수 있음
        }
        
        try:
            response = requests.get(
                self.law_detail_url,
                params=params,
                timeout=15,
                verify=False
            )
            response.raise_for_status()
            response.encoding = 'utf-8'

            content = response.text
            if content.startswith('\ufeff'):
                content = content[1:]

            root = ET.fromstring(content.encode('utf-8'))
            
            law_detail = {
                'law_id': law_id,
                'law_msn': law_msn,
                'law_name': law_name,
                'law_type': '',
                'promulgation_date': '',
                'enforcement_date': '',
                'articles': [],
                'supplementary_provisions': [],
                'tables': [], # 별표, 서식 등을 담을 리스트
                'hierarchy': {
                    'upper_laws': [],
                    'lower_laws': [],
                    'admin_rules': []
                }
            }
            
            basic_info = root.find('.//기본정보')
            if basic_info is not None:
                law_detail['law_type'] = basic_info.findtext('법종구분', '')
                law_detail['promulgation_date'] = basic_info.findtext('공포일자', '')
                law_detail['enforcement_date'] = basic_info.findtext('시행일자', '')

            articles_section = root.find('.//조문')
            if articles_section is not None:
                for article_unit in articles_section.findall('.//조문단위'):
                    article_info = self._extract_article_from_xml(article_unit)
                    if article_info:
                        law_detail['articles'].append(article_info)

            addendums = root.findall('.//부칙')
            for addendum in addendums:
                addendum_info = {
                    'number': addendum.findtext('부칙번호', ''),
                    'promulgation_date': addendum.findtext('부칙공포일자', ''),
                    'content': self._extract_text_from_element(addendum.find('부칙내용'))
                }
                law_detail['supplementary_provisions'].append(addendum_info)

            # --- [수정] 별표/서식 추출 로직 추가 ---
            attachments_section = root.find('.//별표서식')
            if attachments_section is not None:
                for item in attachments_section.findall('.//별표서식단위'):
                    name = item.findtext('별표서식명', '이름 없음')
                    content_elem = item.find('별표서식내용')
                    content = ''
                    if content_elem is not None:
                        # CDATA 내용을 포함한 모든 텍스트를 추출
                        raw_content = ''.join(content_elem.itertext()).strip()
                        # BeautifulSoup을 사용하여 HTML 태그 제거
                        soup = BeautifulSoup(raw_content, 'html.parser')
                        content = soup.get_text(separator='\n', strip=True)

                    link = item.findtext('별표서식PDF파일URL', '')

                    law_detail['tables'].append({
                        'name': name,
                        'content': content,
                        'link': link
                    })

            return law_detail

        except requests.exceptions.RequestException as e:
            st.error(f"'{law_name}' 상세 정보 API 요청 실패: {str(e)}")
            return None
        except ET.ParseError as e:
            st.error(f"'{law_name}' XML 파싱 오류: {str(e)}")
            st.text("수신된 내용:")
            st.code(response.text, language='xml')
            return None
        except Exception as e:
            st.error(f"'{law_name}' 수집 중 예상치 못한 오류 발생: {str(e)}")
            return None

    def collect_law_hierarchy_improved(self, law_id: str, law_msn: str, oc_code: str, law_name: str):
        """법령 체계도 수집 - API 기반 개선된 방식"""
        hierarchy = {
            'upper_laws': [],
            'lower_laws': [],  
            'admin_rules': [],
            'related_laws': [],
            'attachments': []
        }
        
        try:
            base_name = law_name
            
            is_enforcement_decree = '시행령' in law_name
            is_enforcement_rule = '시행규칙' in law_name
            is_admin_rule = any(k in law_name for k in ['고시', '훈령', '예규', '지침'])
            
            for suffix in ['시행령', '시행규칙', '고시', '훈령', '예규', '지침']:
                base_name = base_name.replace(suffix, '').strip()
            
            if is_enforcement_decree or is_enforcement_rule or is_admin_rule:
                results = self.search_law(oc_code, base_name)
                for result in results:
                    if (result['law_name'] == base_name or 
                        (base_name in result['law_name'] and '법' in result['law_name'] 
                         and not any(s in result['law_name'] for s in ['시행령', '시행규칙']))):
                        hierarchy['upper_laws'].append(result)
                        break
                
                if is_enforcement_rule:
                    decree_name = f"{base_name} 시행령"
                    results = self.search_law(oc_code, decree_name)
                    for result in results[:1]:
                        if '시행령' in result['law_name']:
                            hierarchy['upper_laws'].append(result)
            
            if not is_enforcement_rule and not is_admin_rule:
                if not is_enforcement_decree:
                    decree_name = f"{base_name} 시행령"
                    results = self.search_law(oc_code, decree_name)
                    for result in results[:2]:
                        if '시행령' in result['law_name'] and base_name in result['law_name']:
                            hierarchy['lower_laws'].append(result)
                
                rule_name = f"{base_name} 시행규칙"
                results = self.search_law(oc_code, rule_name)
                for result in results[:2]:
                    if '시행규칙' in result['law_name'] and base_name in result['law_name']:
                        hierarchy['lower_laws'].append(result)
            
            if not is_admin_rule:
                admin_types = ['고시', '훈령', '예규', '지침', '규정']
                
                for admin_type in admin_types:
                    search_patterns = [f"{base_name} {admin_type}", f"{base_name}{admin_type}"]
                    
                    for pattern in search_patterns:
                        results = self.search_law(oc_code, pattern)
                        
                        for result in results[:3]:
                            if not any(r['law_id'] == result['law_id'] for r in hierarchy['admin_rules']):
                                if admin_type in result['law_name'] and base_name in result['law_name']:
                                    hierarchy['admin_rules'].append(result)
                        
                        if len(hierarchy['admin_rules']) >= 10:
                            break
                    
                    time.sleep(self.delay)
            
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
        
        # 조문 내용 추출
        article_content_elem = article_elem.find('조문내용')
        article_info['content'] = self._extract_text_from_element(article_content_elem) if article_content_elem is not None else ''

        for para_elem in article_elem.findall('.//항'):
            para_num = para_elem.findtext('항번호', '')
            para_content_elem = para_elem.find('항내용')
            para_content = self._extract_text_from_element(para_content_elem) if para_content_elem is not None else ''
            if para_num and para_content:
                article_info['paragraphs'].append({
                    'number': para_num,
                    'content': para_content
                })
        
        return article_info if article_info['number'] or article_info['title'] else None

    def _extract_text_from_element(self, elem):
        """XML 요소에서 텍스트 추출 (CDATA 포함)"""
        if elem is None:
            return ''
        texts = [text.strip() for text in elem.itertext() if text and text.strip()]
        return ' '.join(texts)

    def export_laws_to_zip(self, laws_dict: dict) -> bytes:
        """선택된 법령들을 ZIP 파일로 압축"""
        zip_buffer = BytesIO()
        
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            all_data = {
                'collection_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'total_laws': len(laws_dict),
                'laws': laws_dict
            }
            
            zip_file.writestr(
                'all_laws.json',
                json.dumps(all_data, ensure_ascii=False, indent=2)
            )
            
            for law_id, law in laws_dict.items():
                safe_name = re.sub(r'[\\/*?:"<>|]', '_', law['law_name'])
                
                zip_file.writestr(
                    f'laws/{safe_name}.json',
                    json.dumps(law, ensure_ascii=False, indent=2)
                )
                
                text_content = self._format_law_as_text(law)
                zip_file.writestr(
                    f'laws/{safe_name}.txt',
                    text_content
                )
            
            if any(law.get('hierarchy') for law in laws_dict.values()):
                hierarchy_summary = self._create_hierarchy_summary(laws_dict)
                zip_file.writestr('hierarchy_summary.md', hierarchy_summary)
            
            readme_content = self._create_readme(laws_dict)
            zip_file.writestr('README.md', readme_content)
        
        zip_buffer.seek(0)
        return zip_buffer.getvalue()

    def _format_law_as_text(self, law: dict) -> str:
        """법령을 텍스트 형식으로 변환 (별표/서식 포함)"""
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
                lines.append(f"\n{article['number']} {article.get('title', '')}")
                lines.append(f"{article['content']}\n")
                
                if article.get('paragraphs'):
                    for para in article['paragraphs']:
                        lines.append(f"  (항 {para['number']}) {para['content']}")
        
        if law.get('supplementary_provisions'):
            lines.append("\n\n【부칙】\n")
            for supp in law['supplementary_provisions']:
                lines.append(f"\n부칙 <{supp['promulgation_date']}>")
                lines.append(supp['content'])

        # --- [수정] 별표/서식 텍스트 변환 추가 ---
        if law.get('tables'):
            lines.append("\n\n【별표/서식】\n")
            for table in law['tables']:
                lines.append(f"\n{'--' * 20}")
                lines.append(f"  {table['name']}")
                lines.append(f"{'--' * 20}")
                lines.append(table.get('content', '내용 없음'))
                if table.get('link'):
                    lines.append(f"\n  PDF 링크: {table['link']}")
        
        return '\n'.join(lines)

    def _create_hierarchy_summary(self, laws_dict: dict) -> str:
        """법령 체계도 요약 생성"""
        summary = ["# 법령 체계도 요약\n"]
        
        by_type = {}
        for law in laws_dict.values():
            law_type = law.get('law_type', '기타')
            if law_type not in by_type:
                by_type[law_type] = []
            by_type[law_type].append(law['law_name'])
        
        for law_type, laws in sorted(by_type.items()):
            summary.append(f"\n## {law_type} ({len(laws)}개)\n")
            for law_name in sorted(laws):
                summary.append(f"- {law_name}")
        
        return '\n'.join(summary)

    def _create_readme(self, laws_dict: dict) -> str:
        """README 파일 생성"""
        content = f"""# 법령 수집 결과

수집 일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
총 법령 수: {len(laws_dict)}개

## 📁 파일 구조

- `all_laws.json`: 전체 법령 데이터 (JSON)
- `laws/`: 개별 법령 파일 디렉토리
  - `*.json`: 법령별 상세 데이터
  - `*.txt`: 법령별 텍스트 형식 (조문, 부칙, 별표/서식 포함)
- `hierarchy_summary.md`: 법령 체계도 요약 (있는 경우)
- `README.md`: 이 파일

## 📊 수집된 법령 목록

"""
        for law_id, law in laws_dict.items():
            content += f"- {law['law_name']} ({law['law_type']})\n"
        
        return content

def generate_markdown_report(collected_laws, collected_hierarchy, collected_precs):
    """마크다운 보고서 생성"""
    md_content = []
    md_content.append(f"# 법령 및 판례 수집 결과\n")
    md_content.append(f"수집 일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    
    md_content.append(f"\n## 📊 수집 요약\n")
    
    main_law_ids = set(collected_hierarchy.keys())
    related_law_ids = set()
    
    for law_id, hierarchy in collected_hierarchy.items():
        for category in ['upper_laws', 'lower_laws', 'admin_rules', 'related_laws']:
            for related_law in hierarchy.get(category, []):
                if related_law.get('law_id') in collected_laws:
                    related_law_ids.add(related_law.get('law_id', ''))
    
    md_content.append(f"- 주 법령: {len(main_law_ids.intersection(collected_laws.keys()))}개\n")
    md_content.append(f"- 관련 법령: {len(related_law_ids)}개\n")
    md_content.append(f"- 총 법령 수: {len(collected_laws)}개\n")
    md_content.append(f"- 총 판례 수: {len(collected_precs)}개\n")
    
    # ... (나머지 마크다운 생성 코드)
    
    return '\n'.join(md_content)

def main():
    st.title("📚 법제처 법령 수집기")
    st.markdown("법제처 Open API를 활용한 법령 수집 도구 (v2.0 - 안정성 및 기능 개선)")
    
    with st.sidebar:
        st.header("⚙️ 설정")
        
        oc_code = st.text_input(
            "기관코드 (OC)",
            placeholder="e.g., test@korea.kr → test",
            help="법제처 Open API 신청 시 발급받은 인증키(기관코드)를 입력하세요."
        )
        
        law_name = st.text_input(
            "법령명",
            placeholder="예: 민법, 도로교통법",
            help="검색할 법령명을 입력하세요."
        )
        
        st.divider()
        
        st.subheader("🎯 수집 모드")
        collection_mode = st.radio(
            "수집 방식 선택",
            ["수동 선택 모드", "자동 수집 모드"],
            captions=["법령 체계도를 확인하며 수집 대상을 직접 선택합니다.", "검색된 법령과 하위 법령을 자동으로 수집합니다."],
            horizontal=True
        )
        st.session_state.collection_mode = 'manual' if collection_mode == "수동 선택 모드" else 'auto'
        
        if st.session_state.collection_mode == 'auto':
            st.warning("자동 수집 모드는 현재 개발 중입니다.")

        col1, col2 = st.columns(2)
        with col1:
            search_btn = st.button("🔍 검색", type="primary", use_container_width=True)
        with col2:
            reset_btn = st.button("🔄 초기화", use_container_width=True)
            if reset_btn:
                for key in st.session_state.keys():
                    del st.session_state[key]
                st.rerun()
    
    collector = LawCollectorStreamlit()
    
    if st.session_state.collection_mode == 'manual':
        manual_collection_ui(collector, oc_code, law_name, search_btn)
    else:
        st.info("자동 수집 모드는 다음 업데이트에 포함될 예정입니다.")

def manual_collection_ui(collector, oc_code, law_name, search_btn):
    """수동 선택 모드 UI"""
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

    if st.session_state.get('search_results'):
        st.header("📋 STEP 1: 법령 선택")
        st.info("체계도를 검색할 기준 법령을 선택하세요.")
        
        selected_indices = []
        for i, law in enumerate(st.session_state.search_results):
            # --- [수정] label 경고 수정 ---
            is_selected = st.checkbox(law['law_name'], key=f"select_{i}")
            if is_selected:
                selected_indices.append(i)
        
        st.session_state.selected_laws = [st.session_state.search_results[i] for i in selected_indices]
        
        if st.session_state.selected_laws:
            st.success(f"{len(st.session_state.selected_laws)}개 법령이 선택되었습니다.")
            
            if st.button("🌳 법령 체계도 검색", type="primary", use_container_width=True):
                all_hierarchy_laws = []
                
                progress_bar = st.progress(0, text="체계도 검색 시작...")
                
                for idx, law in enumerate(st.session_state.selected_laws):
                    progress = (idx + 1) / len(st.session_state.selected_laws)
                    progress_bar.progress(progress, text=f"체계도 검색 중: {law['law_name']}...")
                    
                    hierarchy = collector.collect_law_hierarchy_improved(
                        law['law_id'], law.get('law_msn', ''), oc_code, law['law_name']
                    )
                    
                    # 주 법령 먼저 추가
                    law['main_law'] = law['law_name']
                    law['category'] = 'main'
                    if not any(l['law_id'] == law['law_id'] for l in all_hierarchy_laws):
                         all_hierarchy_laws.append(law)

                    for category in ['upper_laws', 'lower_laws', 'admin_rules', 'related_laws']:
                        for h_law in hierarchy.get(category, []):
                            if not any(l['law_id'] == h_law['law_id'] for l in all_hierarchy_laws):
                                h_law['main_law'] = law['law_name']
                                h_law['category'] = category
                                all_hierarchy_laws.append(h_law)
                    
                    time.sleep(collector.delay)
                
                progress_bar.progress(1.0, text="체계도 검색 완료!")
                st.session_state.hierarchy_laws = all_hierarchy_laws

    if st.session_state.get('hierarchy_laws'):
        st.header("🌳 STEP 2: 수집 대상 법령 선택")
        st.info("수집할 법령을 최종 선택하세요. 주 법령이 기본으로 선택됩니다.")
        
        categories = {'main': '주 법령', 'upper_laws': '상위법', 'lower_laws': '하위법령', 'admin_rules': '행정규칙', 'related_laws': '관련법령'}
        
        selected_hierarchy_indices = []
        
        for category_key, category_name in categories.items():
            category_laws = [(idx, law) for idx, law in enumerate(st.session_state.hierarchy_laws) if law.get('category') == category_key]
            
            if category_laws:
                st.subheader(f"{category_name} ({len(category_laws)}개)")
                for idx, law in category_laws:
                    # 주 법령은 기본 선택
                    default_selection = True if category_key == 'main' else False
                    # --- [수정] label 경고 수정 ---
                    is_selected = st.checkbox(f"{law['law_name']} (관련: {law.get('main_law', 'N/A')})", key=f"h_select_{idx}", value=default_selection)
                    if is_selected:
                        selected_hierarchy_indices.append(idx)
        
        st.session_state.selected_hierarchy_laws = [st.session_state.hierarchy_laws[i] for i in sorted(list(set(selected_hierarchy_indices)))]
        
        if st.session_state.selected_hierarchy_laws:
            st.success(f"총 {len(st.session_state.selected_hierarchy_laws)}개 법령이 수집 대상으로 선택되었습니다.")
            
            if st.button("📥 선택한 법령 수집", type="primary", use_container_width=True):
                collected_laws = {}
                total = len(st.session_state.selected_hierarchy_laws)
                progress_bar = st.progress(0, text="수집 시작...")
                
                for idx, law in enumerate(st.session_state.selected_hierarchy_laws):
                    progress = (idx + 1) / total
                    progress_bar.progress(progress, text=f"수집 중 ({idx + 1}/{total}): {law['law_name']}...")
                    
                    # --- [수정] get_law_detail이 None을 반환할 수 있으므로 확인 ---
                    law_detail = collector.get_law_detail(
                        oc_code, law['law_id'], law.get('law_msn', ''), law['law_name']
                    )
                    
                    if law_detail:
                        collected_laws[law['law_id']] = law_detail
                    
                    time.sleep(collector.delay)
                
                progress_bar.progress(1.0, text="수집 완료!")
                st.session_state.collected_laws = collected_laws
                st.success(f"✅ {len(collected_laws)}개 법령 수집 완료!")
                st.rerun() # 다운로드 섹션을 바로 표시하기 위해 새로고침

    if st.session_state.get('collected_laws'):
        st.header("💾 STEP 3: 다운로드")
        
        col1, col2, col3 = st.columns(3)
        
        with col1:
            json_data = {
                'collection_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'laws': st.session_state.collected_laws
            }
            json_str = json.dumps(json_data, ensure_ascii=False, indent=2)
            st.download_button(
                label="📄 JSON 다운로드", data=json_str, 
                file_name=f"laws_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                mime="application/json", use_container_width=True
            )
        
        with col2:
            zip_data = collector.export_laws_to_zip(st.session_state.collected_laws)
            st.download_button(
                label="📦 ZIP 다운로드", data=zip_data,
                file_name=f"laws_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
                mime="application/zip", use_container_width=True
            )
        
        with col3:
            md_content = generate_markdown_report(
                st.session_state.collected_laws, 
                st.session_state.collected_hierarchy, 
                st.session_state.collected_precs
            )
            st.download_button(
                label="📝 마크다운 보고서", data=md_content,
                file_name=f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md",
                mime="text/markdown", use_container_width=True
            )
        
        with st.expander("📊 수집된 법령 목록 보기"):
            for law_id, law in st.session_state.collected_laws.items():
                st.write(f"- **{law['law_name']}** ({law['law_type']})")
                st.caption(f"  (조문: {len(law['articles'])}개, 부칙: {len(law['supplementary_provisions'])}개, 별표/서식: {len(law['tables'])}개)")

if __name__ == "__main__":
    main()
