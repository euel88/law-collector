"""
법제처 법령 수집기 - ChatGPT API 통합 버전 (v3.0)
- ChatGPT API를 활용한 법령명 추출 정확도 향상
- API 키 선택적 사용 (없어도 기본 기능 동작)
- 사용자 친화적 인터페이스
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
from typing import List, Set, Dict, Optional, Tuple, Any

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
if 'openai_api_key' not in st.session_state:
    st.session_state.openai_api_key = None
if 'use_ai' not in st.session_state:
    st.session_state.use_ai = False


class EnhancedLawFileExtractor:
    """ChatGPT API를 활용한 개선된 법령명 추출 클래스"""
    
    def __init__(self):
        # 제외할 키워드 (카테고리, 설명 등)
        self.exclude_keywords = [
            '상하위법', '행정규칙', '법령', '시행령', '시행규칙', '대통령령', 
            '총리령', '부령', '관한 규정', '상위법', '하위법', '관련법령'
        ]
        
        # 개선된 법령명 패턴 - 행정규칙 우선 배치
        self.law_patterns = [
            # 시행 날짜 패턴을 모든 패턴에 포함
            r'([가-힣]+(?:\s+[가-힣]+)*(?:법|법률|규정|규칙|세칙|분류))\s*\[시행\s*\d{4}\.\s*\d{1,2}\.\s*\d{1,2}\.\]',
            
            # 패턴 1: 독립적인 규정/세칙 (행정규칙) 
            r'^([가-힣]+(?:(?:\s+및\s+)|(?:\s+))?[가-힣]*(?:에\s*관한\s*)?(?:규정|업무규정|감독규정|운영규정|관리규정))(?:\s|$)',
            
            # 패턴 2: 시행세칙 (독립적)
            r'^([가-힣]+(?:(?:\s+및\s+)|(?:\s+))?[가-힣]*(?:업무)?시행세칙)(?:\s|$)',
            
            # 패턴 3: 붙어있는 형태의 규정 처리
            r'([가-힣]+(?:검사및제재에관한|에관한)?규정)(?:\s|$)',
            
            # 패턴 4: 일반적인 법률명 
            r'^([가-힣]+(?:\s+[가-힣]+)*(?:에\s*관한\s*)?(?:특별|기본|관리|촉진|지원|육성|진흥|보호|규제|방지)?법(?:률)?)(?:\s|$)',
            
            # 패턴 5: 시행령 
            r'^([가-힣]+(?:\s+[가-힣]+)*법(?:률)?)\s+시행령(?:\s|$)',
            
            # 패턴 6: 시행규칙 
            r'^([가-힣]+(?:\s+[가-힣]+)*법(?:률)?)\s+시행규칙(?:\s|$)',
            
            # 패턴 7: 규정 + 시행세칙 조합
            r'^([가-힣]+(?:\s+[가-힣]+)*(?:에\s*관한\s*)?규정\s+시행세칙)(?:\s|$)',
            
            # 패턴 8: 분류 (한국표준산업분류 등)
            r'^([가-힣]+(?:\s+[가-힣]+)*분류)(?:\s|$)',
            
            # 패턴 9: 고시, 훈령, 예규
            r'^([가-힣]+(?:\s+[가-힣]+)*(?:에\s*관한\s*)?(?:고시|훈령|예규|지침))(?:\s|$)',
        ]
        
        # AI 설정 확인
        self.use_ai = st.session_state.get('use_ai', False)
        self.api_key = st.session_state.get('openai_api_key', None)
        
    def extract_from_pdf(self, file) -> List[str]:
        """PDF 파일에서 법령명 추출 - ChatGPT API 통합"""
        all_text = ""
        
        try:
            # pdfplumber로 전체 텍스트 추출
            with pdfplumber.open(file) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        all_text += text + "\n"
        except:
            # 실패 시 PyPDF2로 시도
            try:
                file.seek(0)  # 파일 포인터 리셋
                pdf_reader = PyPDF2.PdfReader(file)
                for page in pdf_reader.pages:
                    text = page.extract_text()
                    all_text += text + "\n"
            except Exception as e:
                st.error(f"PDF 읽기 오류: {str(e)}")
                return []
        
        # 기본 추출 로직
        laws = self._extract_laws_from_pdf_structure(all_text)
        
        # ChatGPT API가 설정되어 있으면 사용
        if self.use_ai and self.api_key:
            with st.spinner("🤖 AI가 법령명을 정교화하는 중..."):
                laws = self._enhance_with_chatgpt(all_text, laws)
        
        return sorted(list(laws))
    
    def _enhance_with_chatgpt(self, text: str, initial_laws: Set[str]) -> Set[str]:
        """ChatGPT API를 활용한 법령명 추출 개선"""
        try:
            import openai
            
            # OpenAI 클라이언트 초기화 (새로운 방식)
            from openai import OpenAI
            client = OpenAI(api_key=self.api_key)
            
            # 텍스트 샘플링 (토큰 제한을 위해)
            sample_text = text[:3000]  # 더 많은 컨텍스트 제공
            
            # 프롬프트 개선
            prompt = f"""당신은 한국 법령 전문가입니다. 다음 텍스트에서 실제 법령명을 정확히 추출해주세요.

중요한 규칙:
1. "상하위법", "행정규칙", "관련법령" 같은 카테고리 제목은 제외
2. 법령명은 법제처에서 사용하는 정확한 공식 명칭으로 추출
3. "에관한" → "에 관한"으로 통일
4. 시행령, 시행규칙은 기본 법률명과 함께 완전한 형태로 표기
5. 중복 제거하고 고유한 법령명만 출력
6. 시행 날짜 정보([시행 YYYY.MM.DD.])는 제외

텍스트:
{sample_text}

현재 추출된 법령명 (참고용):
{', '.join(list(initial_laws)[:20])}

정확한 법령명을 한 줄에 하나씩 출력하세요. 법령명만 출력하고 다른 설명은 하지 마세요:"""
            
            # API 호출 (새로운 방식)
            response = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {
                        "role": "system", 
                        "content": "당신은 한국 법령 데이터베이스 전문가입니다. 법제처 형식에 맞는 정확한 법령명만 추출합니다."
                    },
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                max_tokens=1000
            )
            
            # 응답 파싱
            ai_laws = set()
            ai_response = response.choices[0].message.content.strip()
            
            for line in ai_response.split('\n'):
                line = line.strip()
                
                # 번호나 기호 제거
                line = re.sub(r'^[\d\-\.\*\•\·]+\s*', '', line)
                
                # 추가 정제
                line = line.strip('"\'')
                
                if line and self._is_valid_law_name(line) and len(line) > 3:
                    ai_laws.add(line)
            
            # AI가 찾은 법령 수 표시
            new_laws = ai_laws - initial_laws
            if new_laws:
                st.success(f"✨ AI가 {len(new_laws)}개의 추가 법령명을 발견했습니다")
                with st.expander("AI가 추가로 발견한 법령명"):
                    for law in sorted(new_laws):
                        st.write(f"- {law}")
            
            # 기존 결과와 AI 결과 병합
            combined_laws = initial_laws.union(ai_laws)
            
            # AI가 제거한 법령 표시 (잘못 추출된 것들)
            removed_laws = initial_laws - ai_laws
            if removed_laws and len(removed_laws) < len(initial_laws) * 0.3:  # 30% 이하만 제거
                with st.expander("AI가 제외한 항목 (잘못 추출된 것)"):
                    for law in sorted(removed_laws):
                        st.write(f"- {law}")
            
            return combined_laws
                
        except ImportError:
            st.warning("⚠️ OpenAI 라이브러리가 설치되지 않았습니다.")
            st.info("터미널에서 다음 명령을 실행하세요: `pip install openai`")
            return initial_laws
        except Exception as e:
            if "API key" in str(e):
                st.error("❌ API 키가 올바르지 않습니다. 설정을 확인해주세요.")
            else:
                st.warning(f"⚠️ AI 처리 중 오류: {str(e)}")
            return initial_laws
    
    def _extract_laws_from_text(self, text: str) -> Set[str]:
        """텍스트에서 법령명 추출 - 기본 로직"""
        laws = set()
        
        # 텍스트 전처리
        text = self._preprocess_text(text)
        
        # 카테고리와 법령명 분리를 위한 전처리
        text = self._remove_categories(text)
        
        # 모든 패턴으로 법령명 추출
        for pattern in self.law_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE | re.MULTILINE)
            for match in matches:
                law_name = self._clean_law_name(match)
                if self._is_valid_law_name(law_name):
                    laws.add(law_name)
        
        # 특수 케이스 처리
        laws.update(self._extract_compound_laws(text))
        laws.update(self._extract_attached_regulations(text))
        
        # 중복 및 부분 문자열 제거
        laws = self._remove_duplicates_and_substrings(laws)
        
        return laws
    
    def _preprocess_text(self, text: str) -> str:
        """텍스트 전처리"""
        # 연속된 공백을 하나로
        text = re.sub(r'\s+', ' ', text)
        
        # 특정 패턴 사이의 줄바꿈 제거
        text = re.sub(r'([가-힣]+)\s*\n\s*([가-힣]+(?:\s+및\s+)?[가-힣]*(?:에\s*관한)?)', r'\1 \2', text)
        
        # "및" 주변의 공백 정규화
        text = re.sub(r'\s*및\s*', ' 및 ', text)
        
        return text
    
    def _remove_categories(self, text: str) -> str:
        """카테고리 레이블 제거"""
        categories = ['상하위법', '행정규칙', '관련법령', '법령']
        
        for category in categories:
            text = re.sub(rf'^\s*{category}\s*$', '', text, flags=re.MULTILINE)
            text = re.sub(rf'{category}\s+([가-힣]+)', r'\1', text)
            text = re.sub(rf'([가-힣]+)\s+{category}\s+([가-힣]+)', r'\1 \2', text)
        
        return text
    
    def _extract_laws_from_pdf_structure(self, text: str) -> Set[str]:
        """PDF 구조를 고려한 법령명 추출"""
        laws = set()
        
        lines = text.split('\n')
        date_pattern = r'\[시행\s*\d{4}\.\s*\d{1,2}\.\s*\d{1,2}\.\]'
        
        for i, line in enumerate(lines):
            line = line.strip()
            
            # 시행 날짜가 포함된 라인 처리
            if re.search(date_pattern, line):
                law_match = re.match(r'(.+?)\s*\[시행', line)
                if law_match:
                    law_name = law_match.group(1).strip()
                    
                    # 카테고리 제거
                    categories = ['상하위법', '행정규칙', '관련법령', '법령']
                    for cat in categories:
                        law_name = law_name.replace(cat, '').strip()
                    
                    if self._is_valid_law_name(law_name):
                        laws.add(law_name)
            else:
                # 시행 날짜가 없는 경우
                if line in ['상하위법', '행정규칙', '관련법령', '법령']:
                    continue
                
                processed_line = self._preprocess_text(line)
                line_laws = self._extract_from_line(processed_line)
                laws.update(line_laws)
        
        return self._remove_duplicates_and_substrings(laws)
    
    def _extract_from_line(self, line: str) -> Set[str]:
        """한 줄에서 법령명 추출"""
        laws = set()
        
        for pattern in self.law_patterns:
            matches = re.findall(pattern, line, re.IGNORECASE)
            for match in matches:
                law_name = self._clean_law_name(match)
                if self._is_valid_law_name(law_name):
                    laws.add(law_name)
        
        return laws
    
    def _extract_attached_regulations(self, text: str) -> Set[str]:
        """붙어있는 형태의 행정규칙 추출"""
        attached_laws = set()
        
        special_patterns = [
            r'금융기관검사및제재에관한규정',
            r'여신전문금융업감독규정',
            r'여신전문금융업감독업무시행세칙',
            r'[가-힣]+검사및[가-힣]+에관한규정',
            r'[가-힣]+감독업무시행세칙'
        ]
        
        for pattern in special_patterns:
            matches = re.findall(pattern, text)
            for match in matches:
                normalized = match
                normalized = re.sub(r'검사및', '검사 및 ', normalized)
                normalized = re.sub(r'에관한', '에 관한 ', normalized)
                normalized = re.sub(r'업무시행', '업무 시행', normalized)
                
                attached_laws.add(self._clean_law_name(normalized))
        
        return attached_laws
    
    def _clean_law_name(self, law_name: str) -> str:
        """법령명 정제"""
        if not isinstance(law_name, str):
            law_name = str(law_name)
        
        # 시행 정보 제거
        law_name = re.sub(r'\s*\[시행[^\]]+\]', '', law_name)
        
        # 앞뒤 공백 제거
        law_name = law_name.strip()
        
        # 연속된 공백을 하나로
        law_name = ' '.join(law_name.split())
        
        # 붙어있는 형태 정규화
        law_name = re.sub(r'검사및', '검사 및 ', law_name)
        law_name = re.sub(r'에관한', '에 관한 ', law_name)
        
        return law_name
    
    def _is_valid_law_name(self, law_name: str) -> bool:
        """유효한 법령명인지 검증"""
        # 제외 키워드 체크
        if law_name in self.exclude_keywords:
            return False
        
        # 너무 짧은 것 제외
        if len(law_name) < 3:
            return False
        
        # 최소 2글자 이상의 한글이 있어야 함
        korean_chars = re.findall(r'[가-힣]+', law_name)
        if not korean_chars or max(len(k) for k in korean_chars) < 2:
            return False
        
        # 법령 관련 키워드가 포함되어 있어야 함
        law_keywords = ['법', '령', '규칙', '규정', '고시', '훈령', '예규', '지침', '세칙', '분류', '업무규정', '감독규정']
        if not any(keyword in law_name for keyword in law_keywords):
            return False
        
        return True
    
    def _extract_compound_laws(self, text: str) -> Set[str]:
        """합성 법령명 추출"""
        compound_laws = set()
        
        base_law_pattern = r'([가-힣]+(?:\s+[가-힣]+)*법(?:률)?)\s*(?:\[시행[^\]]+\])?\s*\n'
        
        matches = re.finditer(base_law_pattern, text)
        for match in matches:
            base_law = self._clean_law_name(match.group(1))
            
            next_text = text[match.end():match.end() + 200]
            
            if f"{base_law} 시행령" in next_text or f"{base_law}시행령" in next_text:
                compound_laws.add(f"{base_law} 시행령")
            
            if f"{base_law} 시행규칙" in next_text or f"{base_law}시행규칙" in next_text:
                compound_laws.add(f"{base_law} 시행규칙")
        
        return compound_laws
    
    def _remove_duplicates_and_substrings(self, laws: Set[str]) -> Set[str]:
        """중복 및 부분 문자열 제거"""
        laws_list = sorted(list(laws), key=len, reverse=True)
        final_laws = []
        
        for law in laws_list:
            is_substring = False
            for existing_law in final_laws:
                if law in existing_law and law != existing_law:
                    is_substring = True
                    break
            
            if len(law) < 5 and law in ['규정', '세칙', '시행령', '시행규칙']:
                continue
                
            if not is_substring:
                final_laws.append(law)
        
        return set(final_laws)
    
    def extract_from_excel(self, file) -> List[str]:
        """Excel 파일에서 법령명 추출"""
        laws = set()
        
        try:
            excel_file = pd.ExcelFile(file)
            
            for sheet_name in excel_file.sheet_names:
                df = pd.read_excel(file, sheet_name=sheet_name)
                
                all_text = ""
                for column in df.columns:
                    for value in df[column].dropna():
                        if isinstance(value, str):
                            all_text += value + "\n"
                
                # 기본 추출
                sheet_laws = self._extract_laws_from_text(all_text)
                
                # AI 강화 (설정된 경우)
                if self.use_ai and self.api_key and sheet_laws:
                    sheet_laws = self._enhance_with_chatgpt(all_text, sheet_laws)
                
                laws.update(sheet_laws)
                
        except Exception as e:
            st.error(f"Excel 읽기 오류: {str(e)}")
        
        return sorted(list(laws))
    
    def extract_from_markdown(self, file) -> List[str]:
        """Markdown 파일에서 법령명 추출"""
        laws = set()
        
        try:
            content = file.read().decode('utf-8')
            laws = self._extract_laws_from_text(content)
            
            # AI 강화
            if self.use_ai and self.api_key and laws:
                laws = self._enhance_with_chatgpt(content, laws)
                
        except Exception as e:
            st.error(f"Markdown 읽기 오류: {str(e)}")
        
        return sorted(list(laws))
    
    def extract_from_text(self, file) -> List[str]:
        """텍스트 파일에서 법령명 추출"""
        laws = set()
        
        try:
            content = file.read().decode('utf-8')
            laws = self._extract_laws_from_text(content)
            
            # AI 강화
            if self.use_ai and self.api_key and laws:
                laws = self._enhance_with_chatgpt(content, laws)
                
        except Exception as e:
            st.error(f"텍스트 파일 읽기 오류: {str(e)}")
        
        return sorted(list(laws))


class LawCollectorAPI:
    """법령 수집 API 클래스"""
    
    def __init__(self):
        self.law_search_url = "http://www.law.go.kr/DRF/lawSearch.do"
        self.law_detail_url = "http://www.law.go.kr/DRF/lawService.do"
        self.delay = 0.5  # API 호출 간격
        
    def search_law(self, oc_code: str, law_name: str) -> List[Dict[str, Any]]:
        """법령 검색 - 개선된 버전"""
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
            
            if response.status_code != 200:
                st.warning(f"API 응답 코드: {response.status_code}")
                return []
            
            response.encoding = 'utf-8'
            content = response.text
            
            if content.startswith('\ufeff'):
                content = content[1:]
            
            try:
                if not content.strip().startswith('<?xml'):
                    content = '<?xml version="1.0" encoding="UTF-8"?>\n' + content
                
                content = re.sub(r'[\x00-\x08\x0B-\x0C\x0E-\x1F\x7F]', '', content)
                
                root = ET.fromstring(content.encode('utf-8'))
            except ET.ParseError as e:
                st.error(f"XML 파싱 오류: {str(e)[:100]}")
                if '<html>' in content.lower():
                    st.error("API가 HTML을 반환했습니다. 기관코드를 확인해주세요.")
                return []
            
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
            
        except requests.exceptions.Timeout:
            st.error("API 요청 시간 초과 - 나중에 다시 시도해주세요")
            return []
        except requests.exceptions.ConnectionError:
            st.error("네트워크 연결 오류 - 인터넷 연결을 확인해주세요")
            return []
        except Exception as e:
            st.error(f"검색 중 오류 발생: {str(e)}")
            return []
    
    def get_law_detail_with_full_content(self, oc_code: str, law_id: str, law_msn: str, law_name: str) -> Optional[Dict[str, Any]]:
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
            
            basic_info = root.find('.//기본정보')
            if basic_info is not None:
                law_detail['law_type'] = basic_info.findtext('법종구분명', '')
                law_detail['promulgation_date'] = basic_info.findtext('공포일자', '')
                law_detail['enforcement_date'] = basic_info.findtext('시행일자', '')
            
            self._extract_all_articles(root, law_detail)
            self._extract_supplementary_provisions(root, law_detail)
            self._extract_attachments(root, law_detail)
            
            if not law_detail['articles']:
                law_detail['raw_content'] = self._extract_full_text(root)
            
            return law_detail
            
        except Exception as e:
            st.warning(f"{law_name} 수집 중 오류: {str(e)}")
            return None
    
    def _extract_all_articles(self, root: ET.Element, law_detail: Dict[str, Any]) -> None:
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
    
    def _parse_article_unit(self, article_elem: ET.Element) -> Optional[Dict[str, Any]]:
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
    
    def _parse_article_text(self, text: str) -> Optional[Dict[str, Any]]:
        """조문 텍스트 파싱"""
        pattern = r'(제\d+조(?:의\d+)?)\s*(?:\((.*?)\))?\s*(.*?)(?=제\d+조|$)'
        matches = re.findall(pattern, text, re.DOTALL)
        
        if not matches:
            return None
            
        match = matches[0]
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
        
        return article_info
    
    def _extract_article_from_element(self, elem: ET.Element) -> Dict[str, Any]:
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
    
    def _extract_supplementary_provisions(self, root: ET.Element, law_detail: Dict[str, Any]) -> None:
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
    
    def _extract_attachments(self, root: ET.Element, law_detail: Dict[str, Any]) -> None:
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
    
    def _extract_full_text(self, root: ET.Element) -> str:
        """전체 텍스트 추출 (폴백)"""
        return self._get_all_text(root)
    
    def _get_all_text(self, elem: ET.Element) -> str:
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
    
    def export_to_zip(self, laws_dict: Dict[str, Dict[str, Any]]) -> bytes:
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
            
            all_laws_md = self._create_all_laws_markdown(laws_dict)
            zip_file.writestr('all_laws.md', all_laws_md)
            
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
                
                md_content = self._format_law_markdown(law)
                zip_file.writestr(
                    f'laws/{safe_name}.md',
                    md_content
                )
            
            readme = self._create_readme(laws_dict)
            zip_file.writestr('README.md', readme)
        
        zip_buffer.seek(0)
        return zip_buffer.getvalue()
    
    def _format_law_markdown(self, law: Dict[str, Any]) -> str:
        """개별 법령을 Markdown으로 포맷"""
        lines = []
        
        lines.append(f"# {law['law_name']}\n")
        
        lines.append("## 📋 기본 정보\n")
        lines.append(f"- **법종구분**: {law.get('law_type', '')}")
        lines.append(f"- **공포일자**: {law.get('promulgation_date', '')}")
        lines.append(f"- **시행일자**: {law.get('enforcement_date', '')}")
        lines.append(f"- **법령ID**: {law.get('law_id', '')}")
        lines.append("")
        
        if law.get('articles'):
            lines.append("## 📖 조문\n")
            for article in law['articles']:
                lines.append(f"### {article['number']}")
                if article.get('title'):
                    lines.append(f"**{article['title']}**\n")
                
                lines.append(article['content'])
                
                if article.get('paragraphs'):
                    for para in article['paragraphs']:
                        lines.append(f"\n> {para['number']} {para['content']}")
                
                lines.append("")
        
        if law.get('supplementary_provisions'):
            lines.append("\n## 📌 부칙\n")
            for idx, supp in enumerate(law['supplementary_provisions'], 1):
                if supp.get('promulgation_date'):
                    lines.append(f"### 부칙 <{supp['promulgation_date']}>")
                else:
                    lines.append(f"### 부칙 {idx}")
                lines.append(f"\n{supp['content']}\n")
        
        if law.get('attachments'):
            lines.append("\n## 📎 별표/별첨\n")
            for attach in law['attachments']:
                lines.append(f"### [{attach['type']}] {attach.get('title', '')}")
                lines.append(f"\n{attach['content']}\n")
        
        if not law.get('articles') and law.get('raw_content'):
            lines.append("\n## 📄 원문\n")
            lines.append(law['raw_content'])
        
        return '\n'.join(lines)
    
    def _create_all_laws_markdown(self, laws_dict: Dict[str, Dict[str, Any]]) -> str:
        """전체 법령을 하나의 Markdown으로 생성"""
        lines = []
        
        lines.append("# 📚 법령 수집 결과 (전체)\n")
        lines.append(f"**수집 일시**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"**총 법령 수**: {len(laws_dict)}개\n")
        
        lines.append("## 📑 목차\n")
        for idx, (law_id, law) in enumerate(laws_dict.items(), 1):
            anchor = re.sub(r'[^가-힣a-zA-Z0-9]', '', law['law_name'])
            lines.append(f"{idx}. [{law['law_name']}](#{anchor})")
        lines.append("")
        
        lines.append("---\n")
        
        for law_id, law in laws_dict.items():
            anchor = re.sub(r'[^가-힣a-zA-Z0-9]', '', law['law_name'])
            lines.append(f'<div id="{anchor}"></div>\n')
            
            lines.append(self._format_law_markdown(law))
            lines.append("\n---\n")
        
        return '\n'.join(lines)
    
    def export_single_file(self, laws_dict: Dict[str, Dict[str, Any]], format: str = 'json') -> str:
        """선택한 법령들을 하나의 파일로 내보내기"""
        if format == 'json':
            data = {
                'collection_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'total_laws': len(laws_dict),
                'laws': laws_dict
            }
            return json.dumps(data, ensure_ascii=False, indent=2)
        
        elif format == 'markdown':
            return self._create_all_laws_markdown(laws_dict)
        
        elif format == 'text':
            lines = []
            lines.append(f"법령 수집 결과")
            lines.append(f"수집 일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            lines.append(f"총 법령 수: {len(laws_dict)}개")
            lines.append("="*80 + "\n")
            
            for law_id, law in laws_dict.items():
                lines.append(self._format_law_full_text(law))
                lines.append("\n" + "="*80 + "\n")
            
            return '\n'.join(lines)
    
    def _format_law_full_text(self, law: Dict[str, Any]) -> str:
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
    
    def _create_readme(self, laws_dict: Dict[str, Dict[str, Any]]) -> str:
        """README 생성"""
        content = f"""# 법령 수집 결과

수집 일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
총 법령 수: {len(laws_dict)}개

## 📁 파일 구조

- `all_laws.json`: 전체 법령 데이터 (JSON)
- `all_laws.md`: 전체 법령 통합 문서 (Markdown)
- `laws/`: 개별 법령 파일 디렉토리
  - `*.json`: 법령별 상세 데이터
  - `*.txt`: 법령별 전체 텍스트
  - `*.md`: 법령별 Markdown 문서
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


def generate_search_variations(law_name: str) -> List[str]:
    """법령명의 다양한 변형 생성 - 검색 성공률 향상"""
    variations = [law_name]
    
    # 띄어쓰기 추가 버전
    spaced = law_name
    spaced = re.sub(r'([가-힣]+)및([가-힣]+)', r'\1 및 \2', spaced)
    spaced = re.sub(r'([가-힣]+)에관한([가-힣]+)', r'\1에 관한 \2', spaced)
    spaced = re.sub(r'([가-힣]+)에관한', r'\1에 관한 ', spaced)
    if spaced != law_name:
        variations.append(spaced)
    
    # 띄어쓰기 제거 버전
    no_space = law_name.replace(' ', '')
    if no_space != law_name:
        variations.append(no_space)
    
    # "에관한" ↔ "에 관한" 변환
    if '에관한' in law_name:
        variations.append(law_name.replace('에관한', '에 관한'))
    if '에 관한' in law_name:
        variations.append(law_name.replace('에 관한', '에관한'))
    
    # 시행령/시행규칙 분리
    if ' 시행령' in law_name:
        base = law_name.replace(' 시행령', '')
        variations.append(base)
        variations.append(f"{base}시행령")
    
    if ' 시행규칙' in law_name:
        base = law_name.replace(' 시행규칙', '')
        variations.append(base)
        variations.append(f"{base}시행규칙")
    
    # 괄호 제거
    if '(' in law_name or ')' in law_name:
        no_paren = re.sub(r'[()]', '', law_name).strip()
        variations.append(no_paren)
    
    # 주요 키워드만
    words = law_name.split()
    if len(words) > 3:
        variations.append(' '.join(words[:2]))
        if words[-1] in ['법', '령', '규칙', '규정', '세칙']:
            variations.append(' '.join(words[:-1]))
    
    return list(dict.fromkeys(variations))


def is_matching_law(query: str, result_name: str) -> bool:
    """유연한 법령명 매칭"""
    def normalize(text):
        text = re.sub(r'\s+', '', text)
        text = re.sub(r'[^\w가-힣]', '', text)
        return text.lower()
    
    query_norm = normalize(query)
    result_norm = normalize(result_name)
    
    # 정규화된 텍스트로 완전 일치
    if query_norm == result_norm:
        return True
    
    # 포함 관계
    if query_norm in result_norm or result_norm in query_norm:
        return True
    
    # 주요 키워드 매칭
    law_types = ['법률', '법', '시행령', '시행규칙', '규정', '규칙', '세칙', '고시', '훈령', '예규']
    
    query_type = None
    result_type = None
    
    for ltype in law_types:
        if ltype in query:
            query_type = ltype
        if ltype in result_name:
            result_type = ltype
    
    if query_type and result_type and query_type == result_type:
        query_core = query.replace(query_type, '').strip()
        result_core = result_name.replace(result_type, '').strip()
        
        if normalize(query_core) in normalize(result_core) or normalize(result_core) in normalize(query_core):
            return True
    
    # 공통 문자 비율
    common_chars = set(query_norm) & set(result_norm)
    if len(query_norm) > 0:
        similarity = len(common_chars) / len(set(query_norm))
        if similarity >= 0.7:
            return True
    
    return False


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
        
        # AI 설정 섹션 추가
        with st.expander("🤖 AI 설정 (선택사항)", expanded=False):
            st.markdown("**ChatGPT를 사용하여 법령명 추출 정확도를 높입니다**")
            
            api_key = st.text_input(
                "OpenAI API Key",
                type="password",
                value=st.session_state.get('openai_api_key', ''),
                help="OpenAI API 키를 입력하세요. https://platform.openai.com/api-keys 에서 발급 가능합니다."
            )
            
            if api_key:
                st.session_state.openai_api_key = api_key
                st.session_state.use_ai = True
                st.success("✅ API 키가 설정되었습니다!")
                
                # API 키 테스트 버튼
                if st.button("🔍 API 키 테스트", type="secondary"):
                    try:
                        from openai import OpenAI
                        client = OpenAI(api_key=api_key)
                        
                        # 간단한 테스트 요청
                        response = client.chat.completions.create(
                            model="gpt-3.5-turbo",
                            messages=[{"role": "user", "content": "안녕하세요"}],
                            max_tokens=10
                        )
                        st.success("✅ API 키가 정상적으로 작동합니다!")
                    except ImportError:
                        st.error("❌ OpenAI 라이브러리가 설치되지 않았습니다.")
                        st.code("pip install openai", language="bash")
                    except Exception as e:
                        st.error(f"❌ API 키 오류: {str(e)}")
            else:
                st.session_state.use_ai = False
                st.info("💡 API 키를 입력하면 더 정확한 법령명 추출이 가능합니다.")
        
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
            # 세션 상태 초기화 (API 키는 유지)
            keys_to_keep = ['mode', 'openai_api_key', 'use_ai']
            for key in list(st.session_state.keys()):
                if key not in keys_to_keep:
                    del st.session_state[key]
            st.rerun()
    
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
        
        # AI 설정 상태 표시
        if st.session_state.use_ai:
            st.info("🤖 AI 강화 모드가 활성화되었습니다")
        else:
            st.info("💡 AI 설정을 통해 법령명 추출 정확도를 높일 수 있습니다")
        
        extractor = EnhancedLawFileExtractor()
        
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
            
            # 추출된 법령 목록 표시
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
                        st.rerun()
            
            # 법령명 추가
            st.subheader("법령명 추가")
            new_law = st.text_input("새 법령명 입력", key="new_law_input")
            if st.button("➕ 추가") and new_law:
                st.session_state.extracted_laws.append(new_law)
                st.rerun()
            
            # 법령 검색 버튼
            if st.button("🔍 법령 검색", type="primary", use_container_width=True):
                if not oc_code:
                    st.error("기관코드를 입력해주세요!")
                else:
                    # 검색 시작
                    search_results = []
                    progress_bar = st.progress(0)
                    status_text = st.empty()
                    
                    # 수정된 법령명으로 업데이트
                    if edited_laws:
                        st.session_state.extracted_laws = edited_laws
                    
                    total = len(st.session_state.extracted_laws)
                    no_result_laws = []
                    
                    for idx, law_name in enumerate(st.session_state.extracted_laws):
                        progress = (idx + 1) / total
                        progress_bar.progress(progress)
                        status_text.text(f"검색 중: {law_name}")
                        
                        # 다양한 형식으로 검색 시도
                        search_variations_list = generate_search_variations(law_name)
                        found = False
                        
                        for variation in search_variations_list:
                            results = collector.search_law(oc_code, variation)
                            
                            if results:
                                for result in results:
                                    if is_matching_law(law_name, result['law_name']):
                                        result['search_query'] = law_name
                                        search_results.append(result)
                                        found = True
                                        break
                                
                                if found:
                                    break
                        
                        if not found:
                            no_result_laws.append(law_name)
                        
                        time.sleep(collector.delay)
                    
                    progress_bar.progress(1.0)
                    status_text.text("검색 완료!")
                    
                    # 결과 표시
                    if search_results:
                        st.success(f"✅ 총 {len(search_results)}개의 법령을 찾았습니다!")
                        st.session_state.search_results = search_results
                    else:
                        st.warning("검색 결과가 없습니다")
                    
                    # 검색 실패한 법령 목록 표시
                    if no_result_laws:
                        with st.expander(f"❌ 검색되지 않은 법령 ({len(no_result_laws)}개)"):
                            for law in no_result_laws:
                                st.write(f"- {law}")
                            st.info("💡 Tip: 기관코드를 확인하거나, 법령명을 수정해보세요.")
        
        # 검색 결과 표시
        if st.session_state.search_results:
            display_search_results_and_collect(collector, oc_code, is_file_mode=True)


def display_search_results_and_collect(collector: LawCollectorAPI, oc_code: str, is_file_mode: bool = False) -> None:
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
            is_selected = st.checkbox(
                "선택", 
                key=f"sel_{idx}", 
                value=select_all,
                label_visibility="collapsed"
            )
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
                
                # 상세 정보 수집
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
        
        # 다운로드 옵션 선택
        st.subheader("📥 다운로드 옵션")
        download_option = st.radio(
            "다운로드 방식 선택",
            ["개별 파일 (ZIP)", "통합 파일 (단일)"],
            help="개별 파일: 각 법령별로 파일 생성\n통합 파일: 모든 법령을 하나의 파일로"
        )
        
        if download_option == "개별 파일 (ZIP)":
            col1, col2 = st.columns(2)
            
            with col1:
                # JSON 다운로드
                json_data = {
                    'collection_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'mode': st.session_state.mode,
                    'ai_enhanced': st.session_state.use_ai,
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
                    label="📦 ZIP 다운로드 (JSON+TXT+MD)",
                    data=zip_data,
                    file_name=f"laws_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
                    mime="application/zip",
                    use_container_width=True
                )
        
        else:  # 통합 파일
            file_format = st.selectbox(
                "파일 형식 선택",
                ["JSON", "Markdown", "Text"],
                help="모든 법령을 하나의 파일로 통합합니다"
            )
            
            if file_format == "JSON":
                content = collector.export_single_file(st.session_state.collected_laws, 'json')
                mime = "application/json"
                extension = "json"
            elif file_format == "Markdown":
                content = collector.export_single_file(st.session_state.collected_laws, 'markdown')
                mime = "text/markdown"
                extension = "md"
            else:  # Text
                content = collector.export_single_file(st.session_state.collected_laws, 'text')
                mime = "text/plain"
                extension = "txt"
            
            st.download_button(
                label=f"💾 {file_format} 통합 파일 다운로드",
                data=content,
                file_name=f"all_laws_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{extension}",
                mime=mime,
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
                
                # 샘플 조문 표시
                if law.get('articles'):
                    st.write("**샘플 조문:**")
                    sample = law['articles'][0]
                    st.text(f"{sample['number']} {sample.get('title', '')}")
                    content_preview = sample['content'][:200] + "..." if len(sample['content']) > 200 else sample['content']
                    st.text(content_preview)


if __name__ == "__main__":
    main()
