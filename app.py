"""
개선된 법령 수집기 - 파일 업로드 + 직접 검색 통합 버전
법령명 추출 정확도 향상 및 기존 검색 기능 유지
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
import pandas as pd
import openpyxl
import PyPDF2
import pdfplumber

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
if 'mode' not in st.session_state:
    st.session_state.mode = 'direct'  # 'direct' or 'file'
if 'extracted_laws' not in st.session_state:
    st.session_state.extracted_laws = []
if 'search_results' not in st.session_state:
    st.session_state.search_results = []
if 'selected_laws' not in st.session_state:
    st.session_state.selected_laws = []
if 'collected_laws' not in st.session_state:
    st.session_state.collected_laws = {}
if 'file_processed' not in st.session_state:
    st.session_state.file_processed = False

class ImprovedLawFileExtractor:
    """개선된 파일에서 법령명 추출하는 클래스"""
    
    def __init__(self):
        # 제외할 키워드 (카테고리, 설명 등)
        self.exclude_keywords = [
            '상하위법', '행정규칙', '법령', '시행령', '시행규칙', '대통령령', 
            '총리령', '부령', '관한 규정', '상위법', '하위법', '관련법령'
        ]
        
        # 정확한 법령명 패턴 (더 엄격하게)
        self.law_patterns = [
            # 구체적인 법령명 패턴 (2개 이상의 한글 + 법령 접미사)
            r'([가-힣]{2,}(?:에\s*관한\s*)?(?:특별|기본|관리|촉진|지원|육성|진흥|보호|규제|방지)?법(?:률)?)\s*(?:\[시행[^\]]+\])?',
            r'([가-힣]{2,}(?:에\s*관한\s*)?(?:특별|기본|관리|촉진|지원|육성|진흥|보호|규제|방지)?법(?:률)?)\s*시행령\s*(?:\[시행[^\]]+\])?',
            r'([가-힣]{2,}(?:에\s*관한\s*)?(?:특별|기본|관리|촉진|지원|육성|진흥|보호|규제|방지)?법(?:률)?)\s*시행규칙\s*(?:\[시행[^\]]+\])?',
            r'([가-힣]{2,}감독규정)\s*(?:\[시행[^\]]+\])?',
            r'([가-힣]{2,}업무시행세칙)\s*(?:\[시행[^\]]+\])?',
            r'([가-힣]{2,}(?:에\s*관한\s*)?규정)\s*(?:\[시행[^\]]+\])?',
            r'([가-힣]{2,}분류)\s*(?:\[시행[^\]]+\])?',  # 한국표준산업분류 등
        ]
        
    def extract_from_pdf(self, file):
        """PDF 파일에서 법령명 추출 - 개선된 버전"""
        laws = set()
        
        try:
            # pdfplumber 사용
            with pdfplumber.open(file) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        laws.update(self._extract_law_names_improved(text))
        except:
            # 실패 시 PyPDF2로 시도
            try:
                pdf_reader = PyPDF2.PdfReader(file)
                for page in pdf_reader.pages:
                    text = page.extract_text()
                    laws.update(self._extract_law_names_improved(text))
            except Exception as e:
                st.error(f"PDF 읽기 오류: {str(e)}")
        
        return list(laws)
    
    def extract_from_excel(self, file):
        """Excel 파일에서 법령명 추출"""
        laws = set()
        
        try:
            excel_file = pd.ExcelFile(file)
            
            for sheet_name in excel_file.sheet_names:
                df = pd.read_excel(file, sheet_name=sheet_name)
                
                for column in df.columns:
                    for value in df[column].dropna():
                        if isinstance(value, str):
                            laws.update(self._extract_law_names_improved(value))
        except Exception as e:
            st.error(f"Excel 읽기 오류: {str(e)}")
        
        return list(laws)
    
    def extract_from_markdown(self, file):
        """Markdown 파일에서 법령명 추출"""
        laws = set()
        
        try:
            content = file.read().decode('utf-8')
            laws.update(self._extract_law_names_improved(content))
        except Exception as e:
            st.error(f"Markdown 읽기 오류: {str(e)}")
        
        return list(laws)
    
    def extract_from_text(self, file):
        """텍스트 파일에서 법령명 추출"""
        laws = set()
        
        try:
            content = file.read().decode('utf-8')
            laws.update(self._extract_law_names_improved(content))
        except Exception as e:
            st.error(f"텍스트 파일 읽기 오류: {str(e)}")
        
        return list(laws)
    
    def _extract_law_names_improved(self, text):
        """개선된 법령명 추출 - 더 정확하게"""
        laws = set()
        
        # 줄 단위로 처리하여 더 정확한 추출
        lines = text.split('\n')
        
        for line in lines:
            # 각 패턴으로 매칭
            for pattern in self.law_patterns:
                matches = re.findall(pattern, line)
                for match in matches:
                    law_name = match.strip()
                    
                    # 시행 정보 제거
                    law_name = re.sub(r'\s*\[시행[^\]]+\]', '', law_name)
                    
                    # 정제
                    law_name = law_name.replace('\n', ' ').replace('\t', ' ')
                    law_name = ' '.join(law_name.split())
                    
                    # 제외 키워드 체크 (정확히 일치하는 경우만)
                    if law_name in self.exclude_keywords:
                        continue
                    
                    # 너무 짧은 것 제외 (최소 3자 이상)
                    if len(law_name) < 3:
                        continue
                    
                    # 유효성 검증
                    # 1. 최소 2글자 이상의 한글이 법령 접미사 앞에 있어야 함
                    if re.match(r'^[가-힣]{2,}', law_name):
                        laws.add(law_name)
        
        # 중복 제거 및 정렬
        return sorted(list(laws))

class LawCollectorAPI:
    """법령 수집 API 클래스"""
    
    def __init__(self):
        self.law_search_url = "http://www.law.go.kr/DRF/lawSearch.do"
        self.law_detail_url = "http://www.law.go.kr/DRF/lawService.do"
        self.delay = 0.5
        
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
                return []
            
            content = response.text
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
            
        except Exception as e:
            st.error(f"검색 오류: {str(e)}")
            return []
    
    def get_law_detail_with_full_content(self, oc_code: str, law_id: str, law_msn: str, law_name: str):
        """법령 상세 정보 수집 - 조문, 부칙, 별표 모두 포함"""
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
                timeout=30,
                verify=False
            )
            response.encoding = 'utf-8'
            
            if response.status_code != 200:
                st.warning(f"{law_name} 상세 정보 접근 실패")
                return None
            
            content = response.text
            if content.startswith('\ufeff'):
                content = content[1:]
            
            try:
                root = ET.fromstring(content.encode('utf-8'))
            except ET.ParseError:
                st.warning(f"{law_name} XML 파싱 오류")
                return None
            
            # 법령 정보 구조
            law_detail = {
                'law_id': law_id,
                'law_msn': law_msn,
                'law_name': law_name,
                'law_type': '',
                'promulgation_date': '',
                'enforcement_date': '',
                'articles': [],
                'supplementary_provisions': [],
                'attachments': [],
                'raw_content': '',
            }
            
            # 기본 정보
            basic_info = root.find('.//기본정보')
            if basic_info is not None:
                law_detail['law_type'] = basic_info.findtext('법종구분명', '')
                law_detail['promulgation_date'] = basic_info.findtext('공포일자', '')
                law_detail['enforcement_date'] = basic_info.findtext('시행일자', '')
            
            # 조문, 부칙, 별표 추출 (기존 코드 동일)
            self._extract_all_articles(root, law_detail)
            self._extract_supplementary_provisions(root, law_detail)
            self._extract_attachments(root, law_detail)
            
            if not law_detail['articles']:
                law_detail['raw_content'] = self._extract_full_text(root)
            
            return law_detail
            
        except Exception as e:
            st.warning(f"{law_name} 수집 중 오류: {str(e)}")
            return None
    
    # 나머지 메서드들은 기존과 동일...
    def _extract_all_articles(self, root, law_detail):
        """모든 조문 추출"""
        articles_section = root.find('.//조문')
        if articles_section is not None:
            for article_unit in articles_section.findall('.//조문단위'):
                article_info = self._parse_article_unit(article_unit)
                if article_info:
                    law_detail['articles'].append(article_info)
        
        if not law_detail['articles']:
            for article_content in root.findall('.//조문내용'):
                if article_content.text:
                    article_info = self._parse_article_text(article_content.text)
                    if article_info:
                        law_detail['articles'].append(article_info)
        
        if not law_detail['articles']:
            article_elements = []
            for elem in root.iter():
                if elem.tag in ['조', '조문', 'article', '조문단위']:
                    article_elements.append(elem)
            
            for elem in article_elements:
                article_info = self._extract_article_from_element(elem)
                if article_info and article_info['content']:
                    law_detail['articles'].append(article_info)
    
    def _parse_article_unit(self, article_elem):
        """조문단위 파싱"""
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
            article_content = self._get_all_text(article_elem)
        
        article_info['content'] = article_content
        
        for para in article_elem.findall('.//항'):
            para_info = {
                'number': para.findtext('항번호', ''),
                'content': para.findtext('항내용', '')
            }
            if para_info['content']:
                article_info['paragraphs'].append(para_info)
        
        return article_info if (article_info['number'] or article_info['content']) else None
    
    def _parse_article_text(self, text):
        """조문 텍스트 파싱"""
        pattern = r'(제\d+조(?:의\d+)?)\s*(?:\((.*?)\))?\s*(.*?)(?=제\d+조|$)'
        matches = re.findall(pattern, text, re.DOTALL)
        
        articles = []
        for match in matches:
            article_info = {
                'number': match[0],
                'title': match[1],
                'content': match[2].strip(),
                'paragraphs': []
            }
            
            para_pattern = r'([①②③④⑤⑥⑦⑧⑨⑩]+)\s*(.*?)(?=[①②③④⑤⑥⑦⑧⑨⑩]|$)'
            para_matches = re.findall(para_pattern, article_info['content'], re.DOTALL)
            
            for para_match in para_matches:
                article_info['paragraphs'].append({
                    'number': para_match[0],
                    'content': para_match[1].strip()
                })
            
            articles.append(article_info)
        
        return articles[0] if articles else None
    
    def _extract_article_from_element(self, elem):
        """요소에서 조문 정보 추출"""
        article_info = {
            'number': '',
            'title': '',
            'content': '',
            'paragraphs': []
        }
        
        for tag in ['조문번호', '조번호', '번호']:
            num = elem.findtext(tag, '')
            if num:
                article_info['number'] = f"제{num}조" if not num.startswith('제') else num
                break
        
        for tag in ['조문제목', '조제목', '제목']:
            title = elem.findtext(tag, '')
            if title:
                article_info['title'] = title
                break
        
        for tag in ['조문내용', '조내용', '내용']:
            content = elem.findtext(tag, '')
            if content:
                article_info['content'] = content
                break
        
        if not article_info['content']:
            article_info['content'] = self._get_all_text(elem)
        
        return article_info
    
    def _extract_supplementary_provisions(self, root, law_detail):
        """부칙 추출"""
        for addendum in root.findall('.//부칙'):
            addendum_info = {
                'number': addendum.findtext('부칙번호', ''),
                'promulgation_date': addendum.findtext('부칙공포일자', ''),
                'content': self._get_all_text(addendum)
            }
            if addendum_info['content']:
                law_detail['supplementary_provisions'].append(addendum_info)
        
        if not law_detail['supplementary_provisions']:
            for elem in root.iter():
                if elem.tag == '부칙내용' and elem.text:
                    law_detail['supplementary_provisions'].append({
                        'number': '',
                        'promulgation_date': '',
                        'content': elem.text
                    })
    
    def _extract_attachments(self, root, law_detail):
        """별표/별첨 추출"""
        for table in root.findall('.//별표'):
            table_info = {
                'type': '별표',
                'number': table.findtext('별표번호', ''),
                'title': table.findtext('별표제목', ''),
                'content': self._get_all_text(table)
            }
            if table_info['content'] or table_info['title']:
                law_detail['attachments'].append(table_info)
        
        for form in root.findall('.//별지'):
            form_info = {
                'type': '별지',
                'number': form.findtext('별지번호', ''),
                'title': form.findtext('별지제목', ''),
                'content': self._get_all_text(form)
            }
            if form_info['content'] or form_info['title']:
                law_detail['attachments'].append(form_info)
        
        for format_elem in root.findall('.//서식'):
            format_info = {
                'type': '서식',
                'number': format_elem.findtext('서식번호', ''),
                'title': format_elem.findtext('서식제목', ''),
                'content': self._get_all_text(format_elem)
            }
            if format_info['content'] or format_info['title']:
                law_detail['attachments'].append(format_info)
    
    def _extract_full_text(self, root):
        """전체 텍스트 추출"""
        return self._get_all_text(root)
    
    def _get_all_text(self, elem):
        """요소의 모든 텍스트 추출"""
        texts = []
        
        if elem.text and elem.text.strip():
            texts.append(elem.text.strip())
        
        for child in elem:
            child_text = self._get_all_text(child)
            if child_text:
                texts.append(child_text)
            
            if child.tail and child.tail.strip():
                texts.append(child.tail.strip())
        
        return ' '.join(texts)
    
    def export_to_zip(self, laws_dict):
        """수집된 법령을 ZIP으로 내보내기"""
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
                
                text_content = self._format_law_full_text(law)
                zip_file.writestr(
                    f'laws/{safe_name}.txt',
                    text_content
                )
            
            readme = self._create_readme(laws_dict)
            zip_file.writestr('README.md', readme)
        
        zip_buffer.seek(0)
        return zip_buffer.getvalue()
    
    def _format_law_full_text(self, law):
        """법령 전체 내용을 텍스트로 포맷"""
        lines = []
        
        lines.append(f"{'=' * 80}")
        lines.append(f"법령명: {law['law_name']}")
        lines.append(f"법종구분: {law.get('law_type', '')}")
        lines.append(f"공포일자: {law.get('promulgation_date', '')}")
        lines.append(f"시행일자: {law.get('enforcement_date', '')}")
        lines.append(f"{'=' * 80}\n")
        
        if law.get('articles'):
            lines.append("【 조 문 】\n")
            for article in law['articles']:
                lines.append(f"\n{article['number']} {article.get('title', '')}")
                lines.append(article['content'])
                
                if article.get('paragraphs'):
                    for para in article['paragraphs']:
                        lines.append(f"\n  {para['number']} {para['content']}")
                
                lines.append("")
        
        if law.get('supplementary_provisions'):
            lines.append("\n\n【 부 칙 】\n")
            for idx, supp in enumerate(law['supplementary_provisions'], 1):
                if supp.get('promulgation_date'):
                    lines.append(f"\n부칙 <{supp['promulgation_date']}>")
                else:
                    lines.append(f"\n부칙 {idx}")
                lines.append(supp['content'])
        
        if law.get('attachments'):
            lines.append("\n\n【 별표/별첨 】\n")
            for attach in law['attachments']:
                lines.append(f"\n[{attach['type']}] {attach.get('title', '')}")
                lines.append(attach['content'])
                lines.append("")
        
        if not law.get('articles') and law.get('raw_content'):
            lines.append("\n\n【 원 문 】\n")
            lines.append(law['raw_content'])
        
        return '\n'.join(lines)
    
    def _create_readme(self, laws_dict):
        """README 생성"""
        content = f"""# 법령 수집 결과

수집 일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
총 법령 수: {len(laws_dict)}개

## 📁 파일 구조

- `all_laws.json`: 전체 법령 데이터 (JSON)
- `laws/`: 개별 법령 파일 디렉토리
  - `*.json`: 법령별 상세 데이터
  - `*.txt`: 법령별 전체 텍스트 (조문, 부칙, 별표 포함)
- `README.md`: 이 파일

## 📊 수집 통계

"""
        total_articles = 0
        total_provisions = 0
        total_attachments = 0
        
        for law in laws_dict.values():
            total_articles += len(law.get('articles', []))
            total_provisions += len(law.get('supplementary_provisions', []))
            total_attachments += len(law.get('attachments', []))
        
        content += f"- 총 조문 수: {total_articles:,}개\n"
        content += f"- 총 부칙 수: {total_provisions}개\n"
        content += f"- 총 별표/별첨 수: {total_attachments}개\n"
        
        content += "\n## 📖 수집된 법령 목록\n\n"
        
        for law_id, law in laws_dict.items():
            article_count = len(law.get('articles', []))
            content += f"### {law['law_name']}\n"
            content += f"- 법종구분: {law.get('law_type', '')}\n"
            content += f"- 시행일자: {law.get('enforcement_date', '')}\n"
            content += f"- 조문: {article_count}개\n"
            content += f"- 부칙: {len(law.get('supplementary_provisions', []))}개\n"
            content += f"- 별표/별첨: {len(law.get('attachments', []))}개\n\n"
        
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
        
        st.divider()
        
        # 모드 선택
        st.subheader("🎯 수집 방식")
        mode = st.radio(
            "방식 선택",
            ["직접 검색", "파일 업로드"],
            help="직접 검색: 법령명을 입력하여 검색\n파일 업로드: PDF/Excel/MD 파일에서 법령 추출"
        )
        st.session_state.mode = 'direct' if mode == "직접 검색" else 'file'
        
        # 직접 검색 모드
        if st.session_state.mode == 'direct':
            law_name = st.text_input(
                "법령명",
                placeholder="예: 민법, 상법, 형법",
                help="검색할 법령명을 입력하세요"
            )
            
            search_btn = st.button("🔍 검색", type="primary", use_container_width=True)
        
        # 파일 업로드 모드
        else:
            st.subheader("📄 법령체계도 파일 업로드")
            uploaded_file = st.file_uploader(
                "파일 선택",
                type=['pdf', 'xlsx', 'xls', 'md', 'txt'],
                help="PDF, Excel, Markdown, 텍스트 파일을 지원합니다"
            )
            
            if uploaded_file:
                st.success(f"✅ {uploaded_file.name} 업로드됨")
                file_type = uploaded_file.name.split('.')[-1].lower()
                st.info(f"파일 형식: {file_type.upper()}")
        
        # 초기화 버튼
        if st.button("🔄 초기화", type="secondary", use_container_width=True):
            for key in st.session_state:
                if key != 'mode':
                    del st.session_state[key]
            st.experimental_rerun()
    
    # 메인 컨텐츠
    collector = LawCollectorAPI()
    
    # 직접 검색 모드
    if st.session_state.mode == 'direct':
        st.header("🔍 직접 검색 모드")
        
        if 'search_btn' in locals() and search_btn:
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
            display_search_results_and_collect(collector, oc_code)
    
    # 파일 업로드 모드
    else:
        st.header("📄 파일 업로드 모드")
        extractor = ImprovedLawFileExtractor()
        
        # 파일에서 법령 추출
        if uploaded_file and not st.session_state.file_processed:
            st.subheader("📋 STEP 1: 법령명 추출")
            
            with st.spinner("파일에서 법령명을 추출하는 중..."):
                file_type = uploaded_file.name.split('.')[-1].lower()
                
                # 파일 타입별 처리
                if file_type == 'pdf':
                    extracted_laws = extractor.extract_from_pdf(uploaded_file)
                elif file_type in ['xlsx', 'xls']:
                    extracted_laws = extractor.extract_from_excel(uploaded_file)
                elif file_type == 'md':
                    extracted_laws = extractor.extract_from_markdown(uploaded_file)
                elif file_type == 'txt':
                    extracted_laws = extractor.extract_from_text(uploaded_file)
                else:
                    st.error("지원하지 않는 파일 형식입니다")
                    extracted_laws = []
                
                if extracted_laws:
                    st.success(f"✅ {len(extracted_laws)}개의 법령명을 찾았습니다!")
                    st.session_state.extracted_laws = extracted_laws
                    st.session_state.file_processed = True
                else:
                    st.warning("파일에서 법령명을 찾을 수 없습니다")
        
        # 추출된 법령 표시 및 편집
        if st.session_state.extracted_laws:
            st.subheader("✏️ STEP 2: 법령명 확인 및 편집")
            st.info("추출된 법령명을 확인하고 필요시 수정하거나 추가할 수 있습니다")
            
            # 추출된 법령 목록 표시 (읽기 전용)
            st.write("**추출된 법령명:**")
            for idx, law in enumerate(st.session_state.extracted_laws, 1):
                st.write(f"{idx}. {law}")
            
            # 법령명 편집 영역
            edited_laws = []
            
            st.write("\n**법령명 편집:**")
            for idx, law_name in enumerate(st.session_state.extracted_laws):
                col1, col2 = st.columns([4, 1])
                with col1:
                    edited_name = st.text_input(
                        f"법령 {idx+1}",
                        value=law_name,
                        key=f"edit_{idx}"
                    )
                    if edited_name:
                        edited_laws.append(edited_name)
                with col2:
                    if st.button("삭제", key=f"del_{idx}"):
                        st.session_state.extracted_laws.pop(idx)
                        st.experimental_rerun()
            
            # 법령명 추가
            st.subheader("법령명 추가")
            new_law = st.text_input("새 법령명 입력", key="new_law_input")
            if st.button("➕ 추가") and new_law:
                st.session_state.extracted_laws.append(new_law)
                st.experimental_rerun()
            
            # 법령 검색 버튼
            if st.button("🔍 법령 검색", type="primary", use_container_width=True):
                if not oc_code:
                    st.error("기관코드를 입력해주세요!")
                else:
                    search_results = []
                    progress_bar = st.progress(0)
                    status_text = st.empty()
                    
                    if edited_laws:
                        st.session_state.extracted_laws = edited_laws
                    
                    total = len(st.session_state.extracted_laws)
                    
                    for idx, law_name in enumerate(st.session_state.extracted_laws):
                        progress = (idx + 1) / total
                        progress_bar.progress(progress)
                        status_text.text(f"검색 중: {law_name}")
                        
                        results = collector.search_law(oc_code, law_name)
                        
                        for result in results:
                            if law_name in result['law_name'] or result['law_name'] in law_name:
                                result['search_query'] = law_name
                                search_results.append(result)
                        
                        time.sleep(collector.delay)
                    
                    progress_bar.progress(1.0)
                    status_text.text("검색 완료!")
                    
                    if search_results:
                        st.success(f"✅ 총 {len(search_results)}개의 법령을 찾았습니다!")
                        st.session_state.search_results = search_results
                    else:
                        st.warning("검색 결과가 없습니다")
        
        # 검색 결과 표시
        if st.session_state.search_results:
            display_search_results_and_collect(collector, oc_code, is_file_mode=True)


def display_search_results_and_collect(collector, oc_code, is_file_mode=False):
    """검색 결과 표시 및 수집 - 공통 함수"""
    st.subheader("📑 검색 결과")
    
    # 전체 선택
    select_all = st.checkbox("전체 선택", key="select_all_results")
    
    # 테이블 헤더
    if is_file_mode:
        col1, col2, col3, col4, col5 = st.columns([1, 3, 2, 2, 2])
        with col5:
            st.markdown("**검색어**")
    else:
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
    
    # 선택된 법령
    selected_indices = []
    
    for idx, law in enumerate(st.session_state.search_results):
        if is_file_mode:
            col1, col2, col3, col4, col5 = st.columns([1, 3, 2, 2, 2])
        else:
            col1, col2, col3, col4 = st.columns([1, 3, 2, 2])
        
        with col1:
            is_selected = st.checkbox("", key=f"sel_{idx}", value=select_all)
            if is_selected:
                selected_indices.append(idx)
        
        with col2:
            st.write(law['law_name'])
        
        with col3:
            st.write(law.get('law_type', ''))
        
        with col4:
            st.write(law.get('enforcement_date', ''))
        
        if is_file_mode:
            with col5:
                st.write(law.get('search_query', ''))
    
    # 선택된 법령 저장
    st.session_state.selected_laws = [
        st.session_state.search_results[i] for i in selected_indices
    ]
    
    if st.session_state.selected_laws:
        st.success(f"{len(st.session_state.selected_laws)}개 법령이 선택되었습니다")
        
        # 수집 버튼
        if st.button("📥 선택한 법령 수집", type="primary", use_container_width=True):
            collected_laws = {}
            progress_bar = st.progress(0)
            status_text = st.empty()
            
            total = len(st.session_state.selected_laws)
            success_count = 0
            
            for idx, law in enumerate(st.session_state.selected_laws):
                progress = (idx + 1) / total
                progress_bar.progress(progress)
                status_text.text(f"수집 중 ({idx + 1}/{total}): {law['law_name']}")
                
                law_detail = collector.get_law_detail_with_full_content(
                    oc_code,
                    law['law_id'],
                    law.get('law_msn', ''),
                    law['law_name']
                )
                
                if law_detail:
                    collected_laws[law['law_id']] = law_detail
                    success_count += 1
                
                time.sleep(collector.delay)
            
            progress_bar.progress(1.0)
            status_text.text(f"수집 완료! (성공: {success_count}/{total})")
            
            st.session_state.collected_laws = collected_laws
            
            # 통계 표시
            total_articles = sum(len(law.get('articles', [])) for law in collected_laws.values())
            total_provisions = sum(len(law.get('supplementary_provisions', [])) for law in collected_laws.values())
            total_attachments = sum(len(law.get('attachments', [])) for law in collected_laws.values())
            
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("총 조문", f"{total_articles:,}개")
            with col2:
                st.metric("총 부칙", f"{total_provisions}개")
            with col3:
                st.metric("총 별표/별첨", f"{total_attachments}개")
    
    # 다운로드 섹션
    if st.session_state.collected_laws:
        st.header("💾 다운로드")
        
        col1, col2 = st.columns(2)
        
        with col1:
            # JSON 다운로드
            json_data = {
                'collection_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'mode': st.session_state.mode,
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
            zip_data = collector.export_to_zip(st.session_state.collected_laws)
            
            st.download_button(
                label="📦 ZIP 다운로드 (전체 내용)",
                data=zip_data,
                file_name=f"laws_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
                mime="application/zip",
                use_container_width=True
            )
        
        # 수집 결과 상세
        with st.expander("📊 수집 결과 상세"):
            for law_id, law in st.session_state.collected_laws.items():
                st.subheader(law['law_name'])
                col1, col2, col3 = st.columns(3)
                with col1:
                    st.write(f"조문: {len(law.get('articles', []))}개")
                with col2:
                    st.write(f"부칙: {len(law.get('supplementary_provisions', []))}개")
                with col3:
                    st.write(f"별표: {len(law.get('attachments', []))}개")
                
                if law.get('articles'):
                    st.write("**샘플 조문:**")
                    sample = law['articles'][0]
                    st.text(f"{sample['number']} {sample.get('title', '')}")
                    st.text(sample['content'][:200] + "..." if len(sample['content']) > 200 else sample['content'])


if __name__ == "__main__":
    main()
