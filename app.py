"""
법제처 법령 수집기 - 개선된 버전 (v4.0)
- 보안 강화: SSL 인증서 검증
- 성능 개선: 비동기 처리 지원
- 코드 구조 개선: 설정 분리, 에러 처리 강화
- Open API 가이드라인 준수
"""

import streamlit as st
import requests
import xml.etree.ElementTree as ET
import json
import asyncio
import aiohttp
from datetime import datetime
from io import BytesIO
import zipfile
import pandas as pd
import PyPDF2
import pdfplumber
from typing import List, Set, Dict, Optional, Tuple, Any
from dataclasses import dataclass
from functools import lru_cache
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from dotenv import load_dotenv

# 환경 변수 로드
load_dotenv()

# 로깅 설정
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 페이지 설정
st.set_page_config(
    page_title="법제처 법령 수집기",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="expanded"
)

# 설정 클래스
@dataclass
class APIConfig:
    """API 설정을 관리하는 데이터 클래스"""
    # 법제처 API 엔드포인트
    LAW_SEARCH_URL = "https://www.law.go.kr/DRF/lawSearch.do"
    LAW_DETAIL_URL = "https://www.law.go.kr/DRF/lawService.do"
    ADMIN_RULE_SEARCH_URL = "https://www.law.go.kr/DRF/admRulSearch.do"
    ADMIN_RULE_DETAIL_URL = "https://www.law.go.kr/DRF/admRulService.do"
    
    # API 설정
    DEFAULT_DELAY = 0.3  # API 호출 간격 (초)
    MAX_RETRIES = 3      # 최대 재시도 횟수
    TIMEOUT = 30         # 타임아웃 (초)
    MAX_CONCURRENT = 5   # 최대 동시 요청 수
    
    # 페이지당 결과 수
    RESULTS_PER_PAGE = 100

# 법령명 패턴 설정
class LawPatterns:
    """법령명 추출 패턴을 관리하는 클래스"""
    
    # 제외 키워드
    EXCLUDE_KEYWORDS = {
        '상하위법', '행정규칙', '법령', '시행령', '시행규칙', '대통령령', 
        '총리령', '부령', '관한 규정', '상위법', '하위법', '관련법령'
    }
    
    # 법령 타입
    LAW_TYPES = {
        '법', '법률', '시행령', '시행규칙', '규정', '규칙', '세칙', 
        '고시', '훈령', '예규', '지침', '분류', '업무규정', '감독규정'
    }
    
    # 행정규칙 키워드
    ADMIN_KEYWORDS = {
        '규정', '고시', '훈령', '예규', '지침', '세칙', '기준', '요령', '지시'
    }


class EnhancedLawFileExtractor:
    """개선된 법령명 추출 클래스"""
    
    def __init__(self, use_ai: bool = False, api_key: Optional[str] = None):
        self.patterns = LawPatterns()
        self.use_ai = use_ai
        self.api_key = api_key
        self.logger = logging.getLogger(self.__class__.__name__)
        
    def extract_from_file(self, file, file_type: str) -> List[str]:
        """파일 타입에 따른 추출 메서드 디스패치"""
        extractors = {
            'pdf': self._extract_from_pdf,
            'xlsx': self._extract_from_excel,
            'xls': self._extract_from_excel,
            'md': self._extract_from_markdown,
            'txt': self._extract_from_text
        }
        
        extractor = extractors.get(file_type.lower())
        if not extractor:
            raise ValueError(f"지원하지 않는 파일 형식: {file_type}")
            
        return extractor(file)
    
    def _extract_from_pdf(self, file) -> List[str]:
        """PDF 파일에서 법령명 추출"""
        try:
            text = self._read_pdf_content(file)
            laws = self._extract_laws_from_text(text)
            
            if self.use_ai and self.api_key:
                laws = self._enhance_with_ai(text, laws)
                
            return sorted(list(laws))
            
        except Exception as e:
            self.logger.error(f"PDF 추출 오류: {e}")
            return []
    
    def _read_pdf_content(self, file) -> str:
        """PDF 내용 읽기 - pdfplumber 우선, 실패시 PyPDF2"""
        text = ""
        
        # pdfplumber 시도
        try:
            with pdfplumber.open(file) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text += page_text + "\n"
            return text
        except Exception as e:
            self.logger.warning(f"pdfplumber 실패: {e}")
        
        # PyPDF2 폴백
        try:
            file.seek(0)
            pdf_reader = PyPDF2.PdfReader(file)
            for page in pdf_reader.pages:
                text += page.extract_text() + "\n"
            return text
        except Exception as e:
            self.logger.error(f"PyPDF2도 실패: {e}")
            raise
    
    def _extract_laws_from_text(self, text: str) -> Set[str]:
        """텍스트에서 법령명 추출 - 개선된 로직"""
        laws = set()
        
        # 정규화
        text = self._normalize_text(text)
        
        # 라인별 처리
        for line in text.split('\n'):
            line = line.strip()
            
            # 제외 키워드 체크
            if line in self.patterns.EXCLUDE_KEYWORDS:
                continue
                
            # 법령명 추출
            extracted = self._extract_law_names(line)
            laws.update(extracted)
        
        # 후처리
        laws = self._post_process_laws(laws)
        
        return laws
    
    def _normalize_text(self, text: str) -> str:
        """텍스트 정규화"""
        # 연속 공백 제거
        text = ' '.join(text.split())
        
        # 표준화
        replacements = {
            '에관한': '에 관한',
            '및': ' 및 ',
            '·': '·',  # 중점 통일
            '，': ',',  # 쉼표 통일
        }
        
        for old, new in replacements.items():
            text = text.replace(old, new)
            
        return text
    
    def _extract_law_names(self, line: str) -> Set[str]:
        """라인에서 법령명 추출"""
        laws = set()
        
        # 시행 날짜 패턴 제거
        line = self._remove_enforcement_date(line)
        
        # 법령 타입별 매칭
        for law_type in self.patterns.LAW_TYPES:
            if law_type in line:
                # 법령명 경계 찾기
                law_name = self._find_law_boundaries(line, law_type)
                if law_name and self._validate_law_name(law_name):
                    laws.add(law_name)
        
        return laws
    
    def _remove_enforcement_date(self, text: str) -> str:
        """시행 날짜 정보 제거"""
        import re
        return re.sub(r'\[시행\s*\d{4}\.\s*\d{1,2}\.\s*\d{1,2}\.\]', '', text)
    
    def _find_law_boundaries(self, text: str, law_type: str) -> Optional[str]:
        """법령명의 시작과 끝 찾기"""
        import re
        
        # 법령 타입 위치 찾기
        type_pos = text.find(law_type)
        if type_pos == -1:
            return None
            
        # 시작 위치 찾기 (한글로 시작)
        start = 0
        for i in range(type_pos - 1, -1, -1):
            if not (text[i].isalnum() or text[i] in ' ·및관한의에'):
                start = i + 1
                break
        
        # 끝 위치는 법령 타입 뒤
        end = type_pos + len(law_type)
        
        # 시행령/시행규칙 처리
        if end < len(text) - 3:
            next_chars = text[end:end+4]
            if '시행령' in next_chars or '시행규칙' in next_chars:
                end = text.find(' ', end)
                if end == -1:
                    end = len(text)
        
        return text[start:end].strip()
    
    def _validate_law_name(self, law_name: str) -> bool:
        """법령명 유효성 검증"""
        # 길이 체크
        if len(law_name) < 3 or len(law_name) > 100:
            return False
            
        # 제외 키워드 체크
        if law_name in self.patterns.EXCLUDE_KEYWORDS:
            return False
            
        # 한글 포함 체크
        import re
        if not re.search(r'[가-힣]', law_name):
            return False
            
        # 법령 타입 포함 체크
        if not any(law_type in law_name for law_type in self.patterns.LAW_TYPES):
            return False
            
        return True
    
    def _post_process_laws(self, laws: Set[str]) -> Set[str]:
        """법령명 후처리 - 중복 제거, 정규화"""
        processed = set()
        
        # 정렬하여 긴 것부터 처리
        sorted_laws = sorted(laws, key=len, reverse=True)
        
        for law in sorted_laws:
            # 부분 문자열 체크
            is_substring = False
            for existing in processed:
                if law in existing and law != existing:
                    is_substring = True
                    break
                    
            if not is_substring:
                # 최종 정규화
                law = law.strip()
                law = ' '.join(law.split())  # 연속 공백 제거
                processed.add(law)
                
        return processed
    
    def _enhance_with_ai(self, text: str, laws: Set[str]) -> Set[str]:
        """AI를 활용한 법령명 추출 개선"""
        try:
            from openai import OpenAI
            
            client = OpenAI(api_key=self.api_key)
            
            # 텍스트 샘플링 (토큰 제한)
            sample = text[:3000]
            
            # 프롬프트 구성
            prompt = self._create_ai_prompt(sample, laws)
            
            # API 호출
            response = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "한국 법령 데이터베이스 전문가"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                max_tokens=1000
            )
            
            # 응답 파싱
            ai_laws = self._parse_ai_response(response.choices[0].message.content)
            
            # 결과 병합
            return laws.union(ai_laws)
            
        except Exception as e:
            self.logger.error(f"AI 처리 오류: {e}")
            return laws
    
    def _create_ai_prompt(self, text: str, existing_laws: Set[str]) -> str:
        """AI 프롬프트 생성"""
        return f"""다음 텍스트에서 한국 법령명을 정확히 추출하세요.

규칙:
1. 법제처 공식 명칭 사용
2. "상하위법", "관련법령" 같은 카테고리 제외
3. 시행령/시행규칙은 기본법과 함께 표기
4. 한 줄에 하나씩 출력

텍스트:
{text}

현재 추출된 법령 (참고):
{', '.join(list(existing_laws)[:10])}

법령명만 출력:"""
    
    def _parse_ai_response(self, response: str) -> Set[str]:
        """AI 응답 파싱"""
        laws = set()
        
        for line in response.strip().split('\n'):
            line = line.strip()
            
            # 번호, 기호 제거
            import re
            line = re.sub(r'^[\d\-\.\*\•\·]+\s*', '', line)
            line = line.strip('"\'')
            
            if line and self._validate_law_name(line):
                laws.add(line)
                
        return laws
    
    def _extract_from_excel(self, file) -> List[str]:
        """Excel 파일에서 법령명 추출"""
        laws = set()
        
        try:
            excel_file = pd.ExcelFile(file)
            
            for sheet_name in excel_file.sheet_names:
                df = pd.read_excel(file, sheet_name=sheet_name)
                
                # 모든 셀의 텍스트 수집
                text = self._collect_excel_text(df)
                
                # 법령명 추출
                sheet_laws = self._extract_laws_from_text(text)
                laws.update(sheet_laws)
                
        except Exception as e:
            self.logger.error(f"Excel 추출 오류: {e}")
            
        return sorted(list(laws))
    
    def _collect_excel_text(self, df: pd.DataFrame) -> str:
        """DataFrame에서 텍스트 수집"""
        texts = []
        
        for column in df.columns:
            for value in df[column].dropna():
                if isinstance(value, str):
                    texts.append(value)
                    
        return '\n'.join(texts)
    
    def _extract_from_markdown(self, file) -> List[str]:
        """Markdown 파일에서 법령명 추출"""
        try:
            content = file.read().decode('utf-8')
            laws = self._extract_laws_from_text(content)
            return sorted(list(laws))
        except Exception as e:
            self.logger.error(f"Markdown 추출 오류: {e}")
            return []
    
    def _extract_from_text(self, file) -> List[str]:
        """텍스트 파일에서 법령명 추출"""
        try:
            content = file.read().decode('utf-8')
            laws = self._extract_laws_from_text(content)
            return sorted(list(laws))
        except Exception as e:
            self.logger.error(f"텍스트 추출 오류: {e}")
            return []


class LawCollectorAPI:
    """개선된 법령 수집 API 클래스"""
    
    def __init__(self, oc_code: str):
        self.oc_code = oc_code
        self.config = APIConfig()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.session = self._create_session()
        
    def _create_session(self) -> requests.Session:
        """재사용 가능한 세션 생성"""
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        
        # SSL 인증서 검증 활성화 (보안 강화)
        session.verify = True
        
        # 재시도 설정
        from requests.adapters import HTTPAdapter
        from requests.packages.urllib3.util.retry import Retry
        
        retry_strategy = Retry(
            total=self.config.MAX_RETRIES,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504]
        )
        
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        
        return session
    
    def search_laws(self, law_names: List[str], 
                   progress_callback=None) -> List[Dict[str, Any]]:
        """여러 법령을 병렬로 검색"""
        results = []
        
        with ThreadPoolExecutor(max_workers=self.config.MAX_CONCURRENT) as executor:
            # 검색 작업 제출
            future_to_law = {
                executor.submit(self.search_single_law, law_name): law_name
                for law_name in law_names
            }
            
            # 결과 수집
            for idx, future in enumerate(as_completed(future_to_law)):
                law_name = future_to_law[future]
                
                try:
                    result = future.result()
                    results.extend(result)
                    
                    if progress_callback:
                        progress_callback((idx + 1) / len(law_names))
                        
                except Exception as e:
                    self.logger.error(f"{law_name} 검색 오류: {e}")
                    
        return results
    
    def search_single_law(self, law_name: str) -> List[Dict[str, Any]]:
        """단일 법령 검색 - 일반 법령과 행정규칙 모두"""
        results = []
        
        # 일반 법령 검색
        results.extend(self._search_general_law(law_name))
        
        # 행정규칙 검색 (해당하는 경우)
        if any(keyword in law_name for keyword in LawPatterns.ADMIN_KEYWORDS):
            results.extend(self._search_admin_rule(law_name))
            
        # 중복 제거
        return self._remove_duplicates(results)
    
    def _search_general_law(self, law_name: str) -> List[Dict[str, Any]]:
        """일반 법령 검색"""
        params = {
            'OC': self.oc_code,
            'target': 'law',
            'type': 'XML',
            'query': law_name,
            'display': str(self.config.RESULTS_PER_PAGE),
            'page': '1'
        }
        
        try:
            response = self.session.get(
                self.config.LAW_SEARCH_URL,
                params=params,
                timeout=self.config.TIMEOUT
            )
            
            if response.status_code != 200:
                self.logger.warning(f"검색 실패: {law_name} - {response.status_code}")
                return []
                
            # XML 파싱
            laws = self._parse_law_search_response(response.content, law_name)
            return laws
            
        except Exception as e:
            self.logger.error(f"일반 법령 검색 오류: {e}")
            return []
    
    def _search_admin_rule(self, law_name: str) -> List[Dict[str, Any]]:
        """행정규칙 검색"""
        params = {
            'OC': self.oc_code,
            'target': 'admrul',
            'type': 'XML',
            'query': law_name,
            'display': str(self.config.RESULTS_PER_PAGE),
            'page': '1'
        }
        
        try:
            response = self.session.get(
                self.config.ADMIN_RULE_SEARCH_URL,
                params=params,
                timeout=self.config.TIMEOUT
            )
            
            if response.status_code != 200:
                return []
                
            # XML 파싱
            rules = self._parse_admin_rule_response(response.content, law_name)
            return rules
            
        except Exception as e:
            self.logger.error(f"행정규칙 검색 오류: {e}")
            return []
    
    def _parse_law_search_response(self, content: bytes, 
                                  search_query: str) -> List[Dict[str, Any]]:
        """법령 검색 응답 파싱"""
        laws = []
        
        try:
            # XML 파싱
            root = ET.fromstring(content)
            
            for law_elem in root.findall('.//law'):
                law_info = {
                    'law_id': law_elem.findtext('법령ID', ''),
                    'law_msn': law_elem.findtext('법령일련번호', ''),
                    'law_name': law_elem.findtext('법령명한글', ''),
                    'law_type': law_elem.findtext('법종구분', ''),
                    'promulgation_date': law_elem.findtext('공포일자', ''),
                    'enforcement_date': law_elem.findtext('시행일자', ''),
                    'is_admin_rule': False,
                    'search_query': search_query
                }
                
                if law_info['law_id'] and law_info['law_name']:
                    laws.append(law_info)
                    
        except ET.ParseError as e:
            self.logger.error(f"XML 파싱 오류: {e}")
            
        return laws
    
    def _parse_admin_rule_response(self, content: bytes, 
                                  search_query: str) -> List[Dict[str, Any]]:
        """행정규칙 검색 응답 파싱"""
        rules = []
        
        try:
            root = ET.fromstring(content)
            
            for rule_elem in root.findall('.//admrul'):
                rule_info = {
                    'law_id': rule_elem.findtext('행정규칙ID', ''),
                    'law_msn': rule_elem.findtext('행정규칙일련번호', ''),
                    'law_name': rule_elem.findtext('행정규칙명', ''),
                    'law_type': rule_elem.findtext('행정규칙종류', ''),
                    'promulgation_date': rule_elem.findtext('발령일자', ''),
                    'enforcement_date': rule_elem.findtext('시행일자', ''),
                    'is_admin_rule': True,
                    'search_query': search_query
                }
                
                if rule_info['law_id'] and rule_info['law_name']:
                    rules.append(rule_info)
                    
        except ET.ParseError as e:
            self.logger.error(f"행정규칙 XML 파싱 오류: {e}")
            
        return rules
    
    def _remove_duplicates(self, laws: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """중복 제거"""
        seen = set()
        unique_laws = []
        
        for law in laws:
            law_id = law['law_id']
            if law_id not in seen:
                seen.add(law_id)
                unique_laws.append(law)
                
        return unique_laws
    
    def collect_law_details(self, laws: List[Dict[str, Any]], 
                           progress_callback=None) -> Dict[str, Dict[str, Any]]:
        """법령 상세 정보 병렬 수집"""
        collected = {}
        
        with ThreadPoolExecutor(max_workers=self.config.MAX_CONCURRENT) as executor:
            # 수집 작업 제출
            future_to_law = {
                executor.submit(
                    self._get_law_detail,
                    law['law_id'],
                    law['law_msn'],
                    law['law_name'],
                    law['is_admin_rule']
                ): law
                for law in laws
            }
            
            # 결과 수집
            for idx, future in enumerate(as_completed(future_to_law)):
                law = future_to_law[future]
                
                try:
                    detail = future.result()
                    if detail:
                        collected[law['law_id']] = detail
                        
                    if progress_callback:
                        progress_callback((idx + 1) / len(laws))
                        
                except Exception as e:
                    self.logger.error(f"{law['law_name']} 수집 오류: {e}")
                    
        return collected
    
    def _get_law_detail(self, law_id: str, law_msn: str, 
                       law_name: str, is_admin_rule: bool) -> Optional[Dict[str, Any]]:
        """법령 상세 정보 가져오기"""
        if is_admin_rule:
            return self._get_admin_rule_detail(law_id, law_msn, law_name)
        else:
            return self._get_general_law_detail(law_id, law_msn, law_name)
    
    def _get_general_law_detail(self, law_id: str, law_msn: str, 
                               law_name: str) -> Optional[Dict[str, Any]]:
        """일반 법령 상세 정보"""
        params = {
            'OC': self.oc_code,
            'target': 'law',
            'type': 'XML',
            'MST': law_msn
        }
        
        try:
            response = self.session.get(
                self.config.LAW_DETAIL_URL,
                params=params,
                timeout=self.config.TIMEOUT
            )
            
            if response.status_code != 200:
                return None
                
            # 상세 정보 파싱
            return self._parse_law_detail(response.content, law_id, law_msn, law_name)
            
        except Exception as e:
            self.logger.error(f"법령 상세 조회 오류: {e}")
            return None
    
    def _get_admin_rule_detail(self, law_id: str, law_msn: str, 
                              law_name: str) -> Optional[Dict[str, Any]]:
        """행정규칙 상세 정보"""
        params = {
            'OC': self.oc_code,
            'target': 'admrul',
            'type': 'XML',
            'MST': law_msn
        }
        
        try:
            response = self.session.get(
                self.config.ADMIN_RULE_DETAIL_URL,
                params=params,
                timeout=self.config.TIMEOUT
            )
            
            if response.status_code != 200:
                return None
                
            return self._parse_law_detail(response.content, law_id, law_msn, law_name)
            
        except Exception as e:
            self.logger.error(f"행정규칙 상세 조회 오류: {e}")
            return None
    
    def _parse_law_detail(self, content: bytes, law_id: str, 
                         law_msn: str, law_name: str) -> Dict[str, Any]:
        """법령 상세 정보 파싱"""
        detail = {
            'law_id': law_id,
            'law_msn': law_msn,
            'law_name': law_name,
            'law_type': '',
            'promulgation_date': '',
            'enforcement_date': '',
            'articles': [],
            'supplementary_provisions': [],
            'attachments': [],
            'raw_content': ''
        }
        
        try:
            root = ET.fromstring(content)
            
            # 기본 정보
            basic_info = root.find('.//기본정보')
            if basic_info is not None:
                detail['law_type'] = basic_info.findtext('법종구분명', '')
                detail['promulgation_date'] = basic_info.findtext('공포일자', '')
                detail['enforcement_date'] = basic_info.findtext('시행일자', '')
            
            # 조문 추출
            self._extract_articles(root, detail)
            
            # 부칙 추출
            self._extract_supplementary_provisions(root, detail)
            
            # 별표 추출
            self._extract_attachments(root, detail)
            
            # 원문 저장 (조문이 없는 경우)
            if not detail['articles']:
                detail['raw_content'] = self._extract_full_text(root)
                
        except Exception as e:
            self.logger.error(f"상세 정보 파싱 오류: {e}")
            
        return detail
    
    def _extract_articles(self, root: ET.Element, detail: Dict[str, Any]) -> None:
        """조문 추출"""
        articles_section = root.find('.//조문')
        if articles_section is None:
            return
            
        for article_unit in articles_section.findall('.//조문단위'):
            article = {
                'number': article_unit.findtext('조문번호', ''),
                'title': article_unit.findtext('조문제목', ''),
                'content': article_unit.findtext('조문내용', ''),
                'paragraphs': []
            }
            
            # 항 추출
            for para in article_unit.findall('.//항'):
                paragraph = {
                    'number': para.findtext('항번호', ''),
                    'content': para.findtext('항내용', '')
                }
                if paragraph['content']:
                    article['paragraphs'].append(paragraph)
            
            if article['number'] or article['content']:
                detail['articles'].append(article)
    
    def _extract_supplementary_provisions(self, root: ET.Element, 
                                        detail: Dict[str, Any]) -> None:
        """부칙 추출"""
        for addendum in root.findall('.//부칙'):
            provision = {
                'number': addendum.findtext('부칙번호', ''),
                'promulgation_date': addendum.findtext('부칙공포일자', ''),
                'content': self._get_all_text(addendum)
            }
            if provision['content']:
                detail['supplementary_provisions'].append(provision)
    
    def _extract_attachments(self, root: ET.Element, detail: Dict[str, Any]) -> None:
        """별표/별첨 추출"""
        # 별표
        for table in root.findall('.//별표'):
            attachment = {
                'type': '별표',
                'number': table.findtext('별표번호', ''),
                'title': table.findtext('별표제목', ''),
                'content': self._get_all_text(table)
            }
            if attachment['content'] or attachment['title']:
                detail['attachments'].append(attachment)
        
        # 별지
        for form in root.findall('.//별지'):
            attachment = {
                'type': '별지',
                'number': form.findtext('별지번호', ''),
                'title': form.findtext('별지제목', ''),
                'content': self._get_all_text(form)
            }
            if attachment['content'] or attachment['title']:
                detail['attachments'].append(attachment)
    
    def _extract_full_text(self, root: ET.Element) -> str:
        """전체 텍스트 추출"""
        return self._get_all_text(root)
    
    def _get_all_text(self, elem: ET.Element) -> str:
        """요소의 모든 텍스트 추출"""
        texts = []
        
        if elem.text:
            texts.append(elem.text.strip())
            
        for child in elem:
            child_text = self._get_all_text(child)
            if child_text:
                texts.append(child_text)
                
            if child.tail:
                texts.append(child.tail.strip())
                
        return ' '.join(texts)


class LawExporter:
    """법령 내보내기 클래스"""
    
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
    
    def export_to_zip(self, laws_dict: Dict[str, Dict[str, Any]]) -> bytes:
        """ZIP 파일로 내보내기"""
        zip_buffer = BytesIO()
        
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            # 메타데이터
            metadata = {
                'collection_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'total_laws': len(laws_dict),
                'laws': laws_dict
            }
            
            # 전체 JSON
            zip_file.writestr(
                'all_laws.json',
                json.dumps(metadata, ensure_ascii=False, indent=2)
            )
            
            # 전체 Markdown
            all_laws_md = self._create_all_laws_markdown(laws_dict)
            zip_file.writestr('all_laws.md', all_laws_md)
            
            # 개별 파일
            for law_id, law in laws_dict.items():
                safe_name = self._sanitize_filename(law['law_name'])
                
                # JSON
                zip_file.writestr(
                    f'laws/{safe_name}.json',
                    json.dumps(law, ensure_ascii=False, indent=2)
                )
                
                # 텍스트
                text_content = self._format_law_text(law)
                zip_file.writestr(f'laws/{safe_name}.txt', text_content)
                
                # Markdown
                md_content = self._format_law_markdown(law)
                zip_file.writestr(f'laws/{safe_name}.md', md_content)
            
            # README
            readme = self._create_readme(laws_dict)
            zip_file.writestr('README.md', readme)
        
        zip_buffer.seek(0)
        return zip_buffer.getvalue()
    
    def export_single_file(self, laws_dict: Dict[str, Dict[str, Any]], 
                          format: str = 'json') -> str:
        """단일 파일로 내보내기"""
        exporters = {
            'json': self._export_as_json,
            'markdown': self._export_as_markdown,
            'text': self._export_as_text
        }
        
        exporter = exporters.get(format, self._export_as_json)
        return exporter(laws_dict)
    
    def _sanitize_filename(self, filename: str) -> str:
        """파일명 안전하게 변환"""
        import re
        return re.sub(r'[\\/*?:"<>|]', '_', filename)
    
    def _export_as_json(self, laws_dict: Dict[str, Dict[str, Any]]) -> str:
        """JSON 형식으로 내보내기"""
        data = {
            'collection_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'total_laws': len(laws_dict),
            'laws': laws_dict
        }
        return json.dumps(data, ensure_ascii=False, indent=2)
    
    def _export_as_markdown(self, laws_dict: Dict[str, Dict[str, Any]]) -> str:
        """Markdown 형식으로 내보내기"""
        return self._create_all_laws_markdown(laws_dict)
    
    def _export_as_text(self, laws_dict: Dict[str, Dict[str, Any]]) -> str:
        """텍스트 형식으로 내보내기"""
        lines = []
        lines.append("법령 수집 결과")
        lines.append(f"수집 일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"총 법령 수: {len(laws_dict)}개")
        lines.append("=" * 80 + "\n")
        
        for law_id, law in laws_dict.items():
            lines.append(self._format_law_text(law))
            lines.append("\n" + "=" * 80 + "\n")
            
        return '\n'.join(lines)
    
    def _format_law_text(self, law: Dict[str, Any]) -> str:
        """법령을 텍스트로 포맷"""
        lines = []
        
        # 헤더
        lines.append(f"법령명: {law['law_name']}")
        lines.append(f"법종구분: {law.get('law_type', '')}")
        lines.append(f"공포일자: {law.get('promulgation_date', '')}")
        lines.append(f"시행일자: {law.get('enforcement_date', '')}")
        lines.append("-" * 60)
        
        # 조문
        if law.get('articles'):
            lines.append("\n【조 문】\n")
            for article in law['articles']:
                lines.append(f"{article['number']} {article.get('title', '')}")
                lines.append(article['content'])
                
                if article.get('paragraphs'):
                    for para in article['paragraphs']:
                        lines.append(f"  {para['number']} {para['content']}")
                lines.append("")
        
        # 부칙
        if law.get('supplementary_provisions'):
            lines.append("\n【부 칙】\n")
            for provision in law['supplementary_provisions']:
                if provision.get('promulgation_date'):
                    lines.append(f"부칙 <{provision['promulgation_date']}>")
                lines.append(provision['content'])
                lines.append("")
        
        # 별표
        if law.get('attachments'):
            lines.append("\n【별표/별첨】\n")
            for attachment in law['attachments']:
                lines.append(f"[{attachment['type']}] {attachment.get('title', '')}")
                lines.append(attachment['content'])
                lines.append("")
        
        return '\n'.join(lines)
    
    def _format_law_markdown(self, law: Dict[str, Any]) -> str:
        """법령을 Markdown으로 포맷"""
        lines = []
        
        # 제목
        lines.append(f"# {law['law_name']}\n")
        
        # 기본 정보
        lines.append("## 📋 기본 정보\n")
        lines.append(f"- **법종구분**: {law.get('law_type', '')}")
        lines.append(f"- **공포일자**: {law.get('promulgation_date', '')}")
        lines.append(f"- **시행일자**: {law.get('enforcement_date', '')}")
        lines.append("")
        
        # 조문
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
        
        # 부칙
        if law.get('supplementary_provisions'):
            lines.append("## 📌 부칙\n")
            for provision in law['supplementary_provisions']:
                if provision.get('promulgation_date'):
                    lines.append(f"### 부칙 <{provision['promulgation_date']}>")
                lines.append(provision['content'])
                lines.append("")
        
        # 별표
        if law.get('attachments'):
            lines.append("## 📎 별표/별첨\n")
            for attachment in law['attachments']:
                lines.append(f"### [{attachment['type']}] {attachment.get('title', '')}")
                lines.append(attachment['content'])
                lines.append("")
        
        return '\n'.join(lines)
    
    def _create_all_laws_markdown(self, laws_dict: Dict[str, Dict[str, Any]]) -> str:
        """전체 법령 Markdown 생성"""
        lines = []
        
        # 헤더
        lines.append("# 📚 법령 수집 결과\n")
        lines.append(f"**수집 일시**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"**총 법령 수**: {len(laws_dict)}개\n")
        
        # 목차
        lines.append("## 📑 목차\n")
        for idx, (law_id, law) in enumerate(laws_dict.items(), 1):
            anchor = self._sanitize_filename(law['law_name'])
            lines.append(f"{idx}. [{law['law_name']}](#{anchor})")
        lines.append("\n---\n")
        
        # 각 법령
        for law_id, law in laws_dict.items():
            lines.append(self._format_law_markdown(law))
            lines.append("\n---\n")
            
        return '\n'.join(lines)
    
    def _create_readme(self, laws_dict: Dict[str, Dict[str, Any]]) -> str:
        """README 생성"""
        # 통계 계산
        total_articles = sum(len(law.get('articles', [])) for law in laws_dict.values())
        total_provisions = sum(len(law.get('supplementary_provisions', [])) for law in laws_dict.values())
        total_attachments = sum(len(law.get('attachments', [])) for law in laws_dict.values())
        
        content = f"""# 법령 수집 결과

수집 일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
총 법령 수: {len(laws_dict)}개

## 📁 파일 구조

- `all_laws.json`: 전체 법령 데이터 (JSON)
- `all_laws.md`: 전체 법령 통합 문서 (Markdown)
- `laws/`: 개별 법령 파일
  - `*.json`: 법령별 상세 데이터
  - `*.txt`: 법령별 텍스트
  - `*.md`: 법령별 Markdown
- `README.md`: 이 파일

## 📊 통계

- 총 조문 수: {total_articles:,}개
- 총 부칙 수: {total_provisions}개
- 총 별표/별첨 수: {total_attachments}개

## 📖 수집된 법령 목록

"""
        
        for law_id, law in laws_dict.items():
            content += f"\n### {law['law_name']}\n"
            content += f"- 법종구분: {law.get('law_type', '')}\n"
            content += f"- 시행일자: {law.get('enforcement_date', '')}\n"
            content += f"- 조문: {len(law.get('articles', []))}개\n"
            
        return content


# Streamlit UI 헬퍼 함수들
def initialize_session_state():
    """세션 상태 초기화"""
    defaults = {
        'mode': 'direct',
        'extracted_laws': [],
        'search_results': [],
        'selected_laws': [],
        'collected_laws': {},
        'file_processed': False,
        'openai_api_key': None,
        'use_ai': False,
        'oc_code': ''
    }
    
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def show_sidebar():
    """사이드바 UI"""
    with st.sidebar:
        st.header("⚙️ 설정")
        
        # 기관코드
        oc_code = st.text_input(
            "기관코드 (OC)",
            value=st.session_state.get('oc_code', ''),
            placeholder="이메일 @ 앞부분",
            help="예: test@korea.kr → test"
        )
        st.session_state.oc_code = oc_code
        
        st.divider()
        
        # AI 설정
        with st.expander("🤖 AI 설정 (선택사항)", expanded=False):
            st.markdown("**ChatGPT를 사용하여 법령명 추출 정확도를 높입니다**")
            
            api_key = st.text_input(
                "OpenAI API Key",
                type="password",
                value=st.session_state.get('openai_api_key', ''),
                help="https://platform.openai.com/api-keys 에서 발급"
            )
            
            if api_key:
                st.session_state.openai_api_key = api_key
                st.session_state.use_ai = True
                st.success("✅ API 키가 설정되었습니다!")
            else:
                st.session_state.use_ai = False
                st.info("💡 API 키를 입력하면 더 정확한 법령명 추출이 가능합니다.")
        
        st.divider()
        
        # 모드 선택
        st.subheader("🎯 수집 방식")
        mode = st.radio(
            "방식 선택",
            ["직접 검색", "파일 업로드"],
            help="직접 검색: 법령명을 입력하여 검색\n파일 업로드: 파일에서 법령 추출"
        )
        st.session_state.mode = 'direct' if mode == "직접 검색" else 'file'
        
        # 초기화 버튼
        if st.button("🔄 초기화", type="secondary", use_container_width=True):
            keys_to_keep = ['mode', 'openai_api_key', 'use_ai', 'oc_code']
            for key in list(st.session_state.keys()):
                if key not in keys_to_keep:
                    del st.session_state[key]
            st.session_state.file_processed = False
            st.rerun()
        
        return oc_code


def handle_direct_search_mode(oc_code: str):
    """직접 검색 모드 처리"""
    st.header("🔍 직접 검색 모드")
    
    law_name = st.text_input(
        "법령명",
        placeholder="예: 민법, 상법, 형법",
        help="검색할 법령명을 입력하세요"
    )
    
    if st.button("🔍 검색", type="primary", use_container_width=True):
        if not oc_code:
            st.error("기관코드를 입력해주세요!")
        elif not law_name:
            st.error("법령명을 입력해주세요!")
        else:
            with st.spinner(f"'{law_name}' 검색 중..."):
                collector = LawCollectorAPI(oc_code)
                results = collector.search_single_law(law_name)
                
                if results:
                    st.success(f"{len(results)}개의 법령을 찾았습니다!")
                    st.session_state.search_results = results
                else:
                    st.warning("검색 결과가 없습니다.")
                    st.session_state.search_results = []


def handle_file_upload_mode(oc_code: str):
    """파일 업로드 모드 처리"""
    st.header("📄 파일 업로드 모드")
    
    # AI 상태 표시
    if st.session_state.use_ai:
        st.info("🤖 AI 강화 모드가 활성화되었습니다")
    else:
        st.info("💡 AI 설정을 통해 법령명 추출 정확도를 높일 수 있습니다")
    
    uploaded_file = st.file_uploader(
        "파일 선택",
        type=['pdf', 'xlsx', 'xls', 'md', 'txt'],
        help="PDF, Excel, Markdown, 텍스트 파일을 지원합니다"
    )
    
    if uploaded_file and not st.session_state.file_processed:
        st.subheader("📋 STEP 1: 법령명 추출")
        
        with st.spinner("파일에서 법령명을 추출하는 중..."):
            extractor = EnhancedLawFileExtractor(
                use_ai=st.session_state.use_ai,
                api_key=st.session_state.openai_api_key
            )
            
            file_type = uploaded_file.name.split('.')[-1].lower()
            
            try:
                extracted_laws = extractor.extract_from_file(uploaded_file, file_type)
                
                if extracted_laws:
                    st.success(f"✅ {len(extracted_laws)}개의 법령명을 찾았습니다!")
                    st.session_state.extracted_laws = extracted_laws
                    st.session_state.file_processed = True
                else:
                    st.warning("파일에서 법령명을 찾을 수 없습니다")
                    
            except Exception as e:
                st.error(f"파일 처리 오류: {str(e)}")
    
    # 추출된 법령 표시
    if st.session_state.extracted_laws:
        display_extracted_laws(oc_code)


def display_extracted_laws(oc_code: str):
    """추출된 법령 표시 및 편집"""
    st.subheader("✏️ STEP 2: 법령명 확인 및 편집")
    
    # 추출된 법령 목록
    st.write("**추출된 법령명:**")
    for idx, law in enumerate(st.session_state.extracted_laws, 1):
        st.write(f"{idx}. {law}")
    
    # 편집 영역
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
    
    # 검색 버튼
    if st.button("🔍 법령 검색", type="primary", use_container_width=True):
        if not oc_code:
            st.error("기관코드를 입력해주세요!")
        else:
            search_laws_from_list(oc_code, edited_laws or st.session_state.extracted_laws)


def search_laws_from_list(oc_code: str, law_names: List[str]):
    """법령 목록 검색"""
    collector = LawCollectorAPI(oc_code)
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    def update_progress(progress):
        progress_bar.progress(progress)
    
    with st.spinner("법령을 검색하는 중..."):
        results = collector.search_laws(law_names, progress_callback=update_progress)
    
    progress_bar.progress(1.0)
    status_text.text("검색 완료!")
    
    if results:
        st.success(f"✅ 총 {len(results)}개의 법령을 찾았습니다!")
        st.session_state.search_results = results
    else:
        st.warning("검색 결과가 없습니다")


def display_search_results_and_collect(oc_code: str):
    """검색 결과 표시 및 수집"""
    if not st.session_state.search_results:
        return
        
    st.subheader("📑 검색 결과")
    
    # 전체 선택
    select_all = st.checkbox("전체 선택")
    
    # 테이블 헤더
    cols = st.columns([1, 3, 2, 2, 2])
    headers = ["선택", "법령명", "법종구분", "시행일자", "검색어"]
    for col, header in zip(cols, headers):
        col.markdown(f"**{header}**")
    
    st.divider()
    
    # 결과 표시
    selected_indices = []
    for idx, law in enumerate(st.session_state.search_results):
        cols = st.columns([1, 3, 2, 2, 2])
        
        with cols[0]:
            if st.checkbox("", key=f"sel_{idx}", value=select_all):
                selected_indices.append(idx)
        
        with cols[1]:
            st.write(law['law_name'])
        
        with cols[2]:
            st.write(law.get('law_type', ''))
        
        with cols[3]:
            st.write(law.get('enforcement_date', ''))
        
        with cols[4]:
            st.write(law.get('search_query', ''))
    
    # 선택된 법령 저장
    st.session_state.selected_laws = [
        st.session_state.search_results[i] for i in selected_indices
    ]
    
    if st.session_state.selected_laws:
        st.success(f"{len(st.session_state.selected_laws)}개 법령이 선택되었습니다")
        
        # 수집 버튼
        if st.button("📥 선택한 법령 수집", type="primary", use_container_width=True):
            collect_selected_laws(oc_code)


def collect_selected_laws(oc_code: str):
    """선택된 법령 수집"""
    collector = LawCollectorAPI(oc_code)
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    def update_progress(progress):
        progress_bar.progress(progress)
        
    with st.spinner("법령 상세 정보를 수집하는 중..."):
        collected = collector.collect_law_details(
            st.session_state.selected_laws,
            progress_callback=update_progress
        )
    
    progress_bar.progress(1.0)
    status_text.text("수집 완료!")
    
    st.session_state.collected_laws = collected
    
    # 통계 표시
    display_collection_stats(collected)


def display_collection_stats(collected_laws: Dict[str, Dict[str, Any]]):
    """수집 통계 표시"""
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


def display_download_section():
    """다운로드 섹션 표시"""
    if not st.session_state.collected_laws:
        return
        
    st.header("💾 다운로드")
    
    exporter = LawExporter()
    
    # 다운로드 옵션
    st.subheader("📥 다운로드 옵션")
    download_option = st.radio(
        "다운로드 방식 선택",
        ["개별 파일 (ZIP)", "통합 파일 (단일)"],
        help="개별 파일: 각 법령별로 파일 생성\n통합 파일: 모든 법령을 하나의 파일로"
    )
    
    if download_option == "개별 파일 (ZIP)":
        # ZIP 다운로드
        zip_data = exporter.export_to_zip(st.session_state.collected_laws)
        
        st.download_button(
            label="📦 ZIP 다운로드 (JSON+TXT+MD)",
            data=zip_data,
            file_name=f"laws_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
            mime="application/zip",
            use_container_width=True
        )
    else:
        # 통합 파일
        file_format = st.selectbox(
            "파일 형식 선택",
            ["JSON", "Markdown", "Text"]
        )
        
        format_map = {
            "JSON": ("json", "application/json", "json"),
            "Markdown": ("markdown", "text/markdown", "md"),
            "Text": ("text", "text/plain", "txt")
        }
        
        fmt, mime, ext = format_map[file_format]
        content = exporter.export_single_file(st.session_state.collected_laws, fmt)
        
        st.download_button(
            label=f"💾 {file_format} 통합 파일 다운로드",
            data=content,
            file_name=f"all_laws_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}",
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
            
            # 샘플 조문
            if law.get('articles'):
                st.write("**샘플 조문:**")
                sample = law['articles'][0]
                st.text(f"{sample['number']} {sample.get('title', '')}")
                st.text(sample['content'][:200] + "...")


def main():
    """메인 함수"""
    # 세션 상태 초기화
    initialize_session_state()
    
    # 제목
    st.title("📚 법제처 법령 수집기")
    st.markdown("법제처 Open API를 활용한 법령 수집 도구 (v4.0)")
    
    # 사이드바
    oc_code = show_sidebar()
    
    # 모드별 처리
    if st.session_state.mode == 'direct':
        handle_direct_search_mode(oc_code)
    else:
        handle_file_upload_mode(oc_code)
    
    # 검색 결과 표시
    if st.session_state.search_results:
        display_search_results_and_collect(oc_code)
    
    # 다운로드 섹션
    display_download_section()


if __name__ == "__main__":
    main()
