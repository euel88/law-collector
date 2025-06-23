"""
법제처 법령 수집기 - Streamlit 버전
GitHub/Streamlit Cloud에서 실행 가능한 웹 애플리케이션
API 직접 호출 방식으로 수정된 버전
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

class LawCollectorStreamlit:
    """Streamlit용 법령 수집기 - API 직접 호출 방식"""
    
    def __init__(self):
        self.law_search_url = "http://www.law.go.kr/DRF/lawSearch.do"
        self.law_detail_url = "http://www.law.go.kr/DRF/lawService.do"  # API 직접 호출
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
                verify=False
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
                law_msn = law_elem.findtext('법령일련번호', '')  # MSN 추가
                
                if law_id and law_name_full:
                    law_info = {
                        'law_id': law_id,
                        'law_msn': law_msn,  # MSN 저장
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
        """법령 상세 정보 수집 - API 직접 호출 방식"""
        params = {
            'OC': oc_code,
            'target': 'law',
            'type': 'XML',
            'MST': law_msn,  # 법령일련번호 사용
            'mobileYn': 'N'
        }
        
        try:
            # API 호출
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
                # 조문 단위로 처리
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
    
    def _extract_article_from_xml(self, article_elem):
        """XML 요소에서 조문 정보 추출"""
        article_info = {
            'number': '',
            'title': '',
            'content': '',
            'paragraphs': []
        }
        
        # 조문번호 추출
        article_num = article_elem.findtext('조문번호', '')
        if article_num:
            article_info['number'] = f"제{article_num}조"
        
        # 조문제목 추출
        article_info['title'] = article_elem.findtext('조문제목', '')
        
        # 조문내용 추출
        article_content = article_elem.findtext('조문내용', '')
        if not article_content:
            # 조문내용이 없으면 전체 텍스트 추출
            article_content = self._extract_text_from_element(article_elem)
        
        article_info['content'] = article_content
        
        # 항 추출
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
        
        # 모든 텍스트 노드 수집
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
    
    def collect_law_hierarchy(self, law_id: str, law_msn: str, oc_code: str):
        """법령 체계도 수집 - 법제처 웹페이지 직접 스크래핑
        
        법제처의 법령체계도 페이지를 직접 파싱하여 정확한 상하위 관계를 추출합니다.
        패턴 매칭보다 훨씬 정확한 결과를 제공합니다.
        """
        hierarchy = {
            'upper_laws': [],      # 상위법령
            'lower_laws': [],      # 하위법령
            'admin_rules': [],     # 행정규칙
            'related_laws': [],    # 관련법령
            'attachments': []      # 별표/별첨
        }
        
        # 법령 체계도 전용 URL
        # lsStmdInfoP.do는 법령체계도 페이지입니다
        hierarchy_url = f"https://www.law.go.kr/lsStmdInfoP.do?lsiSeq={law_id}"
        
        try:
            # 웹페이지 요청
            st.info(f"🔍 법령 체계도 페이지 접속 중... ({law_id})")
            response = requests.get(
                hierarchy_url,
                timeout=15,
                verify=False,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Connection': 'keep-alive',
                    'Upgrade-Insecure-Requests': '1'
                }
            )
            response.encoding = 'utf-8'
            
            if response.status_code != 200:
                st.warning(f"⚠️ 법령 체계도 페이지 접근 실패 (HTTP {response.status_code})")
                return self._fallback_pattern_search(law_id, law_msn, oc_code)
            
            # BeautifulSoup으로 HTML 파싱
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 법제처 법령체계도 구조 분석
            # 1. 방법 1: 트리 컨테이너 찾기
            tree_found = False
            
            # ID로 찾기
            tree_container = soup.find('div', {'id': 'lawTree'})
            if not tree_container:
                # Class로 찾기
                tree_container = soup.find('div', {'class': 'treeLst'})
            if not tree_container:
                # 다른 가능한 클래스들
                tree_container = soup.find('div', class_=re.compile('tree|stmd|hierarchy'))
            
            if tree_container:
                tree_found = True
                st.success("✅ 법령 체계도 발견!")
                
                # 법령 링크 추출 함수
                def extract_laws_from_section(section, category_name):
                    """섹션에서 법령 링크 추출"""
                    laws = []
                    links = section.find_all('a', href=re.compile(r'lsiSeq=\d+'))
                    
                    for link in links:
                        law_info = self._extract_law_info_from_link(link)
                        if law_info and law_info['law_id'] != law_id:  # 자기 자신 제외
                            laws.append(law_info)
                            st.text(f"  - {category_name}: {law_info['law_name']}")
                    
                    return laws
                
                # 섹션별로 법령 추출
                # 상위법령 섹션
                for keyword in ['상위법령', '모법', '상위', '부모']:
                    upper_section = tree_container.find(text=re.compile(keyword))
                    if upper_section:
                        parent_div = upper_section.find_parent(['div', 'ul', 'li'])
                        if parent_div:
                            hierarchy['upper_laws'] = extract_laws_from_section(parent_div, "상위법")
                            break
                
                # 하위법령 섹션
                for keyword in ['하위법령', '시행령', '시행규칙', '하위']:
                    lower_section = tree_container.find(text=re.compile(keyword))
                    if lower_section:
                        parent_div = lower_section.find_parent(['div', 'ul', 'li'])
                        if parent_div:
                            hierarchy['lower_laws'] = extract_laws_from_section(parent_div, "하위법")
                            break
                
                # 행정규칙 섹션
                for keyword in ['행정규칙', '훈령', '고시', '예규', '지침']:
                    admin_section = tree_container.find(text=re.compile(keyword))
                    if admin_section:
                        parent_div = admin_section.find_parent(['div', 'ul', 'li'])
                        if parent_div:
                            hierarchy['admin_rules'] = extract_laws_from_section(parent_div, "행정규칙")
                            break
            
            # 2. 방법 2: 테이블 구조로 되어있을 경우
            if not tree_found:
                tables = soup.find_all('table', class_=re.compile('stmd|tree|law'))
                for table in tables:
                    rows = table.find_all('tr')
                    for row in rows:
                        cells = row.find_all(['td', 'th'])
                        if len(cells) >= 2:
                            category = cells[0].get_text(strip=True)
                            links_cell = cells[1]
                            
                            if '상위' in category or '모법' in category:
                                links = links_cell.find_all('a')
                                for link in links:
                                    law_info = self._extract_law_info_from_link(link)
                                    if law_info:
                                        hierarchy['upper_laws'].append(law_info)
                            
                            elif '하위' in category or '시행' in category:
                                links = links_cell.find_all('a')
                                for link in links:
                                    law_info = self._extract_law_info_from_link(link)
                                    if law_info:
                                        hierarchy['lower_laws'].append(law_info)
                            
                            elif '행정' in category or '규칙' in category:
                                links = links_cell.find_all('a')
                                for link in links:
                                    law_info = self._extract_law_info_from_link(link)
                                    if law_info:
                                        hierarchy['admin_rules'].append(law_info)
            
            # 3. 방법 3: 모든 법령 링크 추출 후 분류
            if not any([hierarchy['upper_laws'], hierarchy['lower_laws'], hierarchy['admin_rules']]):
                st.info("🔄 대체 방법으로 법령 추출 시도...")
                
                # 모든 법령 링크 찾기
                all_law_links = soup.find_all('a', href=re.compile(r'lsiSeq=\d+'))
                current_law_name = ""
                
                # 현재 법령명 찾기 (페이지 제목 등에서)
                title_elem = soup.find(['h1', 'h2', 'h3'], text=re.compile(r'법령체계도|체계도'))
                if title_elem:
                    current_law_name = title_elem.get_text()
                
                for link in all_law_links:
                    law_info = self._extract_law_info_from_link(link)
                    if law_info and law_info['law_id'] != law_id:
                        # 법령명으로 분류
                        if '시행령' in law_info['law_name']:
                            if current_law_name and '시행령' not in current_law_name:
                                hierarchy['lower_laws'].append(law_info)
                            else:
                                hierarchy['upper_laws'].append(law_info)
                        elif '시행규칙' in law_info['law_name']:
                            hierarchy['lower_laws'].append(law_info)
                        elif any(k in law_info['law_name'] for k in ['고시', '훈령', '예규', '지침']):
                            hierarchy['admin_rules'].append(law_info)
                        else:
                            # 기본 법률로 추정
                            hierarchy['upper_laws'].append(law_info)
            
            # 별표/별첨 검색 (API 활용)
            self._search_attachments_via_api(oc_code, law_id, hierarchy['attachments'])
            
            # 결과 요약
            total_found = (len(hierarchy['upper_laws']) + 
                          len(hierarchy['lower_laws']) + 
                          len(hierarchy['admin_rules']))
            
            if total_found > 0:
                st.success(f"✅ 총 {total_found}개 관련 법령 발견!")
            else:
                st.warning("⚠️ 웹 스크래핑 실패, 대체 방법 사용")
                return self._fallback_pattern_search(law_id, law_msn, oc_code)
            
        except requests.exceptions.Timeout:
            st.error("⏱️ 요청 시간 초과")
            return self._fallback_pattern_search(law_id, law_msn, oc_code)
            
        except Exception as e:
            st.error(f"❌ 법령 체계도 수집 중 오류: {str(e)}")
            return self._fallback_pattern_search(law_id, law_msn, oc_code)
        
        return hierarchy
    
    def _extract_law_info_from_link(self, link_elem):
        """링크 요소에서 법령 정보 추출"""
        try:
            # href에서 법령 ID 추출
            href = link_elem.get('href', '')
            law_id_match = re.search(r'lsiSeq=(\d+)', href)
            if not law_id_match:
                return None
            
            law_id = law_id_match.group(1)
            law_name = link_elem.text.strip()
            
            # 법령 타입 추측
            law_type = ''
            if '법률' in law_name and '시행' not in law_name:
                law_type = '법률'
            elif '시행령' in law_name:
                law_type = '대통령령'
            elif '시행규칙' in law_name:
                law_type = '부령'
            elif '고시' in law_name:
                law_type = '고시'
            elif '훈령' in law_name:
                law_type = '훈령'
            elif '예규' in law_name:
                law_type = '예규'
            
            return {
                'law_id': law_id,
                'law_msn': law_id,  # 일단 동일하게 설정
                'law_name': law_name,
                'law_type': law_type,
                'enforcement_date': ''
            }
        except:
            return None
    
    def _search_attachments_via_api(self, oc_code: str, law_id: str, attachments: list):
        """별표/별첨 API 검색"""
        # 법령 상세 정보에서 별표 정보 추출 시도
        try:
            params = {
                'OC': oc_code,
                'target': 'law',
                'type': 'XML',
                'ID': law_id,
                'mobileYn': 'N'
            }
            
            response = requests.get(
                self.law_detail_url,
                params=params,
                timeout=10,
                verify=False
            )
            
            if response.status_code == 200:
                root = ET.fromstring(response.text.encode('utf-8'))
                
                # 별표 섹션 찾기
                for elem in root.iter():
                    if elem.tag in ['별표', '별지', '서식', '별첨']:
                        attachment_info = {
                            'type': elem.tag,
                            'law_id': f"{law_id}_attach_{elem.tag}",
                            'law_msn': '',
                            'law_name': elem.findtext('제목', f"{elem.tag}"),
                            'description': elem.findtext('내용', '')[:100]
                        }
                        attachments.append(attachment_info)
        except:
            pass
    
    def _fallback_pattern_search(self, law_id: str, law_msn: str, oc_code: str):
        """웹 스크래핑 실패 시 간단한 API 검색으로 폴백"""
        hierarchy = {
            'upper_laws': [],
            'lower_laws': [],
            'admin_rules': [],
            'related_laws': [],
            'attachments': []
        }
        
        try:
            # 현재 법령 정보 가져오기
            law_info = self._get_law_basic_info(oc_code, law_msn)
            if not law_info:
                return hierarchy
            
            law_name = law_info.get('law_name', '')
            
            # 기본 법령명 추출 (시행령, 시행규칙 제거)
            base_name = law_name
            for suffix in ['시행령', '시행규칙']:
                base_name = base_name.replace(suffix, '').strip()
            
            # 간단한 검색만 수행
            if '시행령' in law_name:
                # 시행령인 경우: 상위 법률과 하위 시행규칙 검색
                results = self.search_law(oc_code, base_name)
                for result in results[:3]:
                    if result['law_name'] == base_name:
                        hierarchy['upper_laws'].append(result)
                
                results = self.search_law(oc_code, f"{base_name} 시행규칙")
                for result in results[:3]:
                    hierarchy['lower_laws'].append(result)
            
            elif '시행규칙' in law_name:
                # 시행규칙인 경우: 상위 법률과 시행령 검색
                results = self.search_law(oc_code, base_name)
                for result in results[:3]:
                    if result['law_name'] == base_name or '시행령' in result['law_name']:
                        hierarchy['upper_laws'].append(result)
            
            else:
                # 법률인 경우: 하위 시행령, 시행규칙 검색
                for suffix in ['시행령', '시행규칙']:
                    results = self.search_law(oc_code, f"{law_name} {suffix}")
                    for result in results[:3]:
                        hierarchy['lower_laws'].append(result)
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
    """마크다운 보고서 생성 - 체계도 정보 강화"""
    md_content = []
    md_content.append(f"# 법령 및 판례 수집 결과\n")
    md_content.append(f"수집 일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    
    # 수집 요약
    md_content.append(f"\n## 📊 수집 요약\n")
    
    # 주 법령과 관련 법령 구분
    main_law_ids = set()
    related_law_ids = set()
    
    for law_id, hierarchy in collected_hierarchy.items():
        main_law_ids.add(law_id)
        for category in ['upper_laws', 'lower_laws', 'admin_rules']:
            for related_law in hierarchy.get(category, []):
                related_law_ids.add(related_law.get('law_id', ''))
    
    md_content.append(f"- 주 법령: {len(main_law_ids)}개\n")
    md_content.append(f"- 관련 법령: {len(related_law_ids)}개\n")
    md_content.append(f"- 총 법령 수: {len(collected_laws)}개\n")
    md_content.append(f"- 총 판례 수: {len(collected_precs)}개\n")
    
    # 법령별 체계도
    md_content.append(f"\n## 🌳 법령 체계도\n")
    
    for law_id in main_law_ids:
        if law_id in collected_laws:
            law = collected_laws[law_id]
            md_content.append(f"\n### 📋 {law['law_name']} 체계도\n")
            
            if law_id in collected_hierarchy:
                hierarchy = collected_hierarchy[law_id]
                
                # 체계도 시각화 (텍스트 기반)
                md_content.append("```")
                md_content.append(f"         [{law['law_name']}]")
                md_content.append(f"              |")
                
                # 상위법
                if hierarchy.get('upper_laws'):
                    md_content.append(f"      상위법 ↑")
                    for upper in hierarchy['upper_laws'][:3]:
                        md_content.append(f"    • {upper['law_name']}")
                
                # 하위법령
                if hierarchy.get('lower_laws'):
                    md_content.append(f"              |")
                    md_content.append(f"      하위법령 ↓")
                    for lower in hierarchy['lower_laws'][:5]:
                        md_content.append(f"    • {lower['law_name']}")
                
                # 행정규칙
                if hierarchy.get('admin_rules'):
                    md_content.append(f"              |")
                    md_content.append(f"     행정규칙 ↓")
                    for admin in hierarchy['admin_rules'][:5]:
                        md_content.append(f"    • {admin['law_name']}")
                
                # 별표/별첨
                if hierarchy.get('attachments'):
                    md_content.append(f"              |")
                    md_content.append(f"    별표/별첨 ↓")
                    for attach in hierarchy['attachments'][:3]:
                        md_content.append(f"    • {attach['law_name']} ({attach['type']})")
                
                md_content.append("```\n")
    
    # 상세 법령 정보
    md_content.append(f"\n## 📖 법령 상세 정보\n")
    
    # 주 법령 먼저
    md_content.append(f"\n### 주 법령\n")
    for law_id in main_law_ids:
        if law_id in collected_laws:
            law = collected_laws[law_id]
            md_content.append(f"\n#### {law['law_name']}\n")
            md_content.append(f"- 법령 ID: {law_id}\n")
            md_content.append(f"- 법종구분: {law['law_type']}\n")
            md_content.append(f"- 공포일자: {law['promulgation_date']}\n")
            md_content.append(f"- 시행일자: {law['enforcement_date']}\n")
            
            # 조문 요약
            if law.get('articles'):
                md_content.append(f"- 조문 수: {len(law['articles'])}개\n")
                md_content.append(f"\n##### 주요 조문\n")
                for i, article in enumerate(law['articles'][:5]):
                    md_content.append(f"\n###### {article['number']} {article['title']}\n")
                    content = article['content'][:200] + '...' if len(article['content']) > 200 else article['content']
                    md_content.append(f"{content}\n")
    
    # 관련 법령
    if related_law_ids:
        md_content.append(f"\n### 관련 법령\n")
        for law_id in related_law_ids:
            if law_id in collected_laws:
                law = collected_laws[law_id]
                md_content.append(f"\n#### {law['law_name']}\n")
                md_content.append(f"- 법종구분: {law['law_type']}\n")
                md_content.append(f"- 시행일자: {law['enforcement_date']}\n")
                md_content.append(f"- 조문 수: {len(law.get('articles', []))}개\n")
    
    # 통계 정보
    md_content.append(f"\n## 📈 통계 정보\n")
    
    # 법령 타입별 분류
    law_types = {}
    for law in collected_laws.values():
        law_type = law.get('law_type', '기타')
        law_types[law_type] = law_types.get(law_type, 0) + 1
    
    md_content.append(f"\n### 법령 타입별 분류\n")
    for law_type, count in sorted(law_types.items(), key=lambda x: x[1], reverse=True):
        md_content.append(f"- {law_type}: {count}개\n")
    
    # 총 조문 수
    total_articles = sum(len(law.get('articles', [])) for law in collected_laws.values())
    total_provisions = sum(len(law.get('supplementary_provisions', [])) for law in collected_laws.values())
    
    md_content.append(f"\n### 수집 내용 통계\n")
    md_content.append(f"- 총 조문 수: {total_articles:,}개\n")
    md_content.append(f"- 총 부칙 수: {total_provisions}개\n")
    
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
        auto_collect_hierarchy = st.checkbox(
            "체계도 법령 자동 수집",
            value=False,
            help="상위법, 하위법령, 규칙 등을 자동으로 함께 수집합니다"
        )
        include_attachments = st.checkbox(
            "별표/별첨 포함",
            value=False,
            help="법령의 별표, 별지, 서식 등을 검색하여 포함합니다"
        )
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
        
        # 각 법령에 대한 체크박스와 정보 표시
        for i, law in enumerate(st.session_state.search_results):
            col1, col2, col3, col4 = st.columns([1, 3, 2, 2])
            
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
            # 진행 상황 표시
            progress_bar = st.progress(0)
            status_text = st.empty()
            
            # 초기화
            st.session_state.collected_laws = {}
            st.session_state.collected_hierarchy = {}
            st.session_state.collected_precs = []
            
            # 전체 작업 계산
            total_steps = len(st.session_state.selected_laws)
            if include_hierarchy:
                total_steps += len(st.session_state.selected_laws)
            
            current_step = 0
            
            # 법령 수집
            for law in st.session_state.selected_laws:
                current_step += 1
                progress = current_step / total_steps
                progress_bar.progress(progress)
                status_text.text(f"수집 중: {law['law_name']}...")
                
                # 법령 상세 정보 수집
                law_detail = collector.get_law_detail(
                    oc_code,
                    law['law_id'],
                    law.get('law_msn', ''),
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
                    
                    hierarchy = collector.collect_law_hierarchy(
                        law['law_id'],
                        law.get('law_msn', ''),
                        oc_code
                    )
                    if hierarchy:
                        st.session_state.collected_hierarchy[law['law_id']] = hierarchy
                        law_detail['hierarchy'] = hierarchy
                        
                        # 체계도 법령 자동 수집
                        if auto_collect_hierarchy:
                            with st.expander(f"🔄 {law['law_name']} 관련 법령 수집 중...", expanded=True):
                                all_related_laws = []
                                
                                # 모든 관련 법령 수집
                                for category in ['upper_laws', 'lower_laws', 'admin_rules']:
                                    all_related_laws.extend(hierarchy.get(category, []))
                                
                                # 별표/별첨 추가
                                if include_attachments:
                                    all_related_laws.extend(hierarchy.get('attachments', []))
                                
                                # 관련 법령 수집
                                for idx, related_law in enumerate(all_related_laws):
                                    col1, col2 = st.columns([3, 1])
                                    with col1:
                                        st.text(f"📖 {related_law['law_name']}")
                                    with col2:
                                        st.text(related_law.get('law_type', ''))
                                    
                                    # 관련 법령 상세 정보 수집
                                    if related_law.get('law_msn'):
                                        related_detail = collector.get_law_detail(
                                            oc_code,
                                            related_law['law_id'],
                                            related_law['law_msn'],
                                            related_law['law_name']
                                        )
                                        
                                        if related_detail:
                                            st.session_state.collected_laws[related_law['law_id']] = related_detail
                                            st.success(f"✓ {related_law['law_name']} 수집 완료")
                                        
                                        time.sleep(collector.delay)
                
                # API 부하 방지
                time.sleep(collector.delay)
            
            progress_bar.progress(1.0)
            status_text.text("수집 완료!")
            
            # 수집 결과 요약
            total_collected = len(st.session_state.collected_laws)
            hierarchy_count = sum(
                len(h.get('upper_laws', [])) + 
                len(h.get('lower_laws', [])) + 
                len(h.get('admin_rules', [])) + 
                len(h.get('attachments', []))
                for h in st.session_state.collected_hierarchy.values()
            )
            
            st.success(f"""
            ✅ 수집 완료!
            - 총 {total_collected}개 법령 수집
            - {hierarchy_count}개 관련 법령 발견
            """)
            
            # 수집 완료
            if auto_collect_hierarchy and len(st.session_state.collected_hierarchy) > 0:
                st.divider()
                col1, col2 = st.columns(2)
                
                with col1:
                    if st.button("📦 체계도 전체 ZIP 다운로드", type="primary", use_container_width=True):
                        with st.spinner("ZIP 파일 생성 중..."):
                            # 모든 수집된 법령을 ZIP으로 압축
                            zip_data = collector.export_all_laws_to_zip(st.session_state.collected_laws)
                            
                            # 다운로드 버튼
                            st.download_button(
                                label="💾 law_collection.zip 다운로드",
                                data=zip_data,
                                file_name=f"law_collection_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
                                mime="application/zip",
                                use_container_width=True
                            )
                
                with col2:
                    if st.button("🔄 추가 체계도 수집", type="secondary", use_container_width=True):
                        st.experimental_rerun()
    
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
                    st.write(f"- 공포일자: {law.get('promulgation_date', '')}")
                    st.write(f"- 시행일자: {law.get('enforcement_date', '')}")
                    st.write(f"- 조문 수: {len(law.get('articles', []))}개")
                    st.write(f"- 부칙 수: {len(law.get('supplementary_provisions', []))}개")
        
        with tab2:
            # 법령 내용 표시
            st.subheader("법령 내용")
            
            # 법령 선택
            law_names = [law['law_name'] for law in st.session_state.collected_laws.values()]
            if law_names:
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
                                    
                                    # 항 표시
                                    if article.get('paragraphs'):
                                        st.write("\n**항:**")
                                        for para in article['paragraphs']:
                                            st.write(f"- 제{para['number']}항: {para['content']}")
                        else:
                            st.info("조문 정보가 없습니다.")
                        
                        # 부칙 표시
                        if law.get('supplementary_provisions'):
                            st.subheader("부칙")
                            for supp in law['supplementary_provisions']:
                                with st.expander(f"부칙 ({supp['promulgation_date']})"):
                                    st.write(supp['content'])
                        
                        break
        
        with tab3:
            # 법령 체계도 시각화
            st.subheader("법령 체계도")
            
            if not st.session_state.collected_hierarchy:
                st.info("법령 체계도를 수집하려면 '법령 체계도 포함' 옵션을 활성화하세요.")
            else:
                # 체계도가 있는 법령 목록
                laws_with_hierarchy = [
                    law for law_id, law in st.session_state.collected_laws.items()
                    if law_id in st.session_state.collected_hierarchy
                ]
                
                if laws_with_hierarchy:
                    selected_law_for_hierarchy = st.selectbox(
                        "체계도를 볼 법령 선택",
                        options=[law['law_name'] for law in laws_with_hierarchy],
                        key="hierarchy_selector"
                    )
                    
                    # 선택된 법령의 체계도 표시
                    for law_id, law in st.session_state.collected_laws.items():
                        if law['law_name'] == selected_law_for_hierarchy:
                            hierarchy = st.session_state.collected_hierarchy.get(law_id, {})
                            
                            # 체계도 시각화
                            st.markdown(f"### 📊 {law['law_name']} 체계도")
                            
                            # 체계도 요약
                            col1, col2, col3, col4 = st.columns(4)
                            with col1:
                                st.metric("상위법", len(hierarchy.get('upper_laws', [])))
                            with col2:
                                st.metric("하위법령", len(hierarchy.get('lower_laws', [])))
                            with col3:
                                st.metric("행정규칙", len(hierarchy.get('admin_rules', [])))
                            with col4:
                                st.metric("별표/별첨", len(hierarchy.get('attachments', [])))
                            
                            # 시각적 체계도 (텍스트 기반)
                            with st.container():
                                st.markdown("```")
                                st.text(f"                    [{law['law_name']}]")
                                st.text("                           |")
                                
                                if hierarchy.get('upper_laws'):
                                    st.text("                    상위법 ↑")
                                    for upper in hierarchy['upper_laws'][:3]:
                                        st.text(f"          • {upper['law_name']}")
                                    if len(hierarchy['upper_laws']) > 3:
                                        st.text(f"          ... 외 {len(hierarchy['upper_laws'])-3}개")
                                
                                st.text("                           |")
                                st.text("                    ----+----")
                                st.text("                    |       |")
                                
                                if hierarchy.get('lower_laws'):
                                    st.text("             하위법령↓       ")
                                    for lower in hierarchy['lower_laws'][:3]:
                                        st.text(f"          • {lower['law_name']}")
                                    if len(hierarchy['lower_laws']) > 3:
                                        st.text(f"          ... 외 {len(hierarchy['lower_laws'])-3}개")
                                
                                if hierarchy.get('admin_rules'):
                                    st.text("                          행정규칙↓")
                                    for admin in hierarchy['admin_rules'][:3]:
                                        st.text(f"                       • {admin['law_name']}")
                                    if len(hierarchy['admin_rules']) > 3:
                                        st.text(f"                       ... 외 {len(hierarchy['admin_rules'])-3}개")
                                
                                st.markdown("```")
                            
                            # 상세 목록
                            tab3_1, tab3_2, tab3_3, tab3_4 = st.tabs(["상위법", "하위법령", "행정규칙", "별표/별첨"])
                            
                            with tab3_1:
                                if hierarchy.get('upper_laws'):
                                    for upper in hierarchy['upper_laws']:
                                        col1, col2, col3 = st.columns([3, 2, 1])
                                        with col1:
                                            st.write(f"📜 {upper['law_name']}")
                                        with col2:
                                            st.write(upper.get('law_type', ''))
                                        with col3:
                                            if upper['law_id'] in st.session_state.collected_laws:
                                                st.success("✓ 수집됨")
                                            else:
                                                st.info("미수집")
                                else:
                                    st.info("상위법이 없습니다.")
                            
                            with tab3_2:
                                if hierarchy.get('lower_laws'):
                                    for lower in hierarchy['lower_laws']:
                                        col1, col2, col3 = st.columns([3, 2, 1])
                                        with col1:
                                            st.write(f"📋 {lower['law_name']}")
                                        with col2:
                                            st.write(lower.get('law_type', ''))
                                        with col3:
                                            if lower['law_id'] in st.session_state.collected_laws:
                                                st.success("✓ 수집됨")
                                            else:
                                                st.info("미수집")
                                else:
                                    st.info("하위법령이 없습니다.")
                            
                            with tab3_3:
                                if hierarchy.get('admin_rules'):
                                    for admin in hierarchy['admin_rules']:
                                        col1, col2, col3 = st.columns([3, 2, 1])
                                        with col1:
                                            st.write(f"📑 {admin['law_name']}")
                                        with col2:
                                            st.write(admin.get('law_type', ''))
                                        with col3:
                                            if admin['law_id'] in st.session_state.collected_laws:
                                                st.success("✓ 수집됨")
                                            else:
                                                st.info("미수집")
                                else:
                                    st.info("행정규칙이 없습니다.")
                            
                            with tab3_4:
                                if hierarchy.get('attachments'):
                                    for attach in hierarchy['attachments']:
                                        col1, col2, col3 = st.columns([3, 1, 1])
                                        with col1:
                                            st.write(f"📎 {attach['law_name']}")
                                        with col2:
                                            st.write(attach['type'])
                                        with col3:
                                            if attach['law_id'] in st.session_state.collected_laws:
                                                st.success("✓ 수집됨")
                                            else:
                                                st.info("미수집")
                                else:
                                    st.info("별표/별첨이 없습니다.")
                            
                            break
        
        with tab4:
            # 다운로드
            st.subheader("수집 결과 다운로드")
            
            # 다운로드 옵션
            col1, col2 = st.columns(2)
            
            with col1:
                st.markdown("### 📄 개별 다운로드")
                
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
            
            with col2:
                st.markdown("### 📦 일괄 다운로드")
                
                # ZIP 다운로드 옵션
                if st.session_state.collected_laws:
                    # 체계도가 있는 법령 선택
                    laws_with_hierarchy = [
                        law_id for law_id in st.session_state.collected_laws.keys()
                        if law_id in st.session_state.collected_hierarchy
                    ]
                    
                    if laws_with_hierarchy:
                        selected_law_id = st.selectbox(
                            "체계도 법령 선택",
                            options=laws_with_hierarchy,
                            format_func=lambda x: st.session_state.collected_laws[x]['law_name']
                        )
                        
                        if st.button("🚀 체계도 전체 다운로드", type="primary", use_container_width=True):
                            with st.spinner("체계도의 모든 법령을 수집 중..."):
                                # 선택된 법령과 체계도 정보
                                main_law = st.session_state.collected_laws[selected_law_id]
                                hierarchy = st.session_state.collected_hierarchy[selected_law_id]
                                
                                # 모든 관련 법령 수집
                                all_related_laws = collector.download_all_related_laws(
                                    oc_code,
                                    main_law,
                                    hierarchy,
                                    include_attachments=True
                                )
                                
                                if all_related_laws:
                                    # ZIP 파일 생성
                                    with st.spinner("ZIP 파일 생성 중..."):
                                        zip_data = collector.export_all_laws_to_zip(all_related_laws)
                                    
                                    # 다운로드 버튼
                                    st.download_button(
                                        label=f"💾 {main_law['law_name']}_체계도_전체.zip",
                                        data=zip_data,
                                        file_name=f"{main_law['law_name']}_hierarchy_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
                                        mime="application/zip",
                                        use_container_width=True
                                    )
                    else:
                        st.info("체계도가 있는 법령을 먼저 수집해주세요.")
                else:
                    st.info("법령을 먼저 수집해주세요.")
            
            # 미리보기
            st.divider()
            with st.expander("📝 마크다운 미리보기"):
                st.markdown(md_content[:2000] + "..." if len(md_content) > 2000 else md_content)

if __name__ == "__main__":
    main()
