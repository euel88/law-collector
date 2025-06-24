"""
법제처 법령 수집기 - 버그 수정 완료 (v6.1)
- OpenAI API 키 사이드바 직접 입력 방식 유지
- 행정규칙 API URL 수정
- 접근성 경고 해결
- 보안 강화
"""

import streamlit as st
import requests
import xml.etree.ElementTree as ET
import json
import time
import re
import os
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
from pathlib import Path

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 페이지 설정
st.set_page_config(
    page_title="📚 법제처 법령 수집기",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ===== 설정 클래스 =====
@dataclass
class APIConfig:
    """API 설정을 관리하는 데이터 클래스"""
    # 법제처 API 엔드포인트 (수정됨)
    LAW_SEARCH_URL = "https://www.law.go.kr/DRF/lawSearch.do"
    LAW_DETAIL_URL = "https://www.law.go.kr/DRF/lawService.do"
    # 행정규칙 API URL 수정 (admRulSc로 변경)
    ADMIN_RULE_SEARCH_URL = "https://www.law.go.kr/DRF/admRulSc.do"
    ADMIN_RULE_DETAIL_URL = "https://www.law.go.kr/DRF/admRulInfoR.do"
    
    # API 설정
    DEFAULT_DELAY = 0.3  # API 호출 간격 (초)
    MAX_RETRIES = 3      # 최대 재시도 횟수
    TIMEOUT = 30         # 타임아웃 (초)
    MAX_CONCURRENT = 5   # 최대 동시 요청 수
    
    # 페이지당 결과 수
    RESULTS_PER_PAGE = 100


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
    
    # 법령명 패턴 (정규표현식)
    LAW_PATTERNS = [
        # 시행 날짜 포함 패턴
        r'([가-힣]+(?:\s+[가-힣]+)*(?:법|법률|규정|규칙|세칙|분류))\s*\[시행\s*\d{4}\.\s*\d{1,2}\.\s*\d{1,2}\.\]',
        # 독립적인 규정/세칙
        r'^([가-힣]+(?:(?:\s+및\s+)|(?:\s+))?[가-힣]*(?:에\s*관한\s*)?(?:규정|업무규정|감독규정|운영규정|관리규정))(?:\s|$)',
        # 시행세칙
        r'^([가-힣]+(?:(?:\s+및\s+)|(?:\s+))?[가-힣]*(?:업무)?시행세칙)(?:\s|$)',
        # 붙어있는 형태
        r'([가-힣]+(?:검사및제재에관한|에관한)?규정)(?:\s|$)',
        # 일반 법률
        r'^([가-힣]+(?:\s+[가-힣]+)*(?:에\s*관한\s*)?(?:특별|기본|관리|촉진|지원|육성|진흥|보호|규제|방지)?법(?:률)?)(?:\s|$)',
        # 시행령/시행규칙
        r'^([가-힣]+(?:\s+[가-힣]+)*법(?:률)?)\s+시행령(?:\s|$)',
        r'^([가-힣]+(?:\s+[가-힣]+)*법(?:률)?)\s+시행규칙(?:\s|$)',
    ]


# ===== 파일에서 법령명 추출 클래스 =====
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
            st.error(f"PDF 파일 처리 중 오류가 발생했습니다: {str(e)}")
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
        """텍스트에서 법령명 추출"""
        laws = set()
        
        # 텍스트 정규화
        text = self._normalize_text(text)
        
        # 패턴 매칭으로 법령명 추출
        for pattern in self.patterns.LAW_PATTERNS:
            matches = re.findall(pattern, text, re.MULTILINE | re.IGNORECASE)
            for match in matches:
                law_name = self._clean_law_name(match)
                if self._validate_law_name(law_name):
                    laws.add(law_name)
        
        # 라인별 추가 처리
        for line in text.split('\n'):
            line = line.strip()
            if line in self.patterns.EXCLUDE_KEYWORDS:
                continue
            
            # 법령 타입별 매칭
            for law_type in self.patterns.LAW_TYPES:
                if law_type in line:
                    law_name = self._extract_law_name_from_line(line, law_type)
                    if law_name and self._validate_law_name(law_name):
                        laws.add(law_name)
        
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
    
    def _extract_law_name_from_line(self, line: str, law_type: str) -> Optional[str]:
        """라인에서 법령명 추출"""
        # 법령 타입 위치 찾기
        type_pos = line.find(law_type)
        if type_pos == -1:
            return None
            
        # 시작 위치 찾기 (한글로 시작)
        start = 0
        for i in range(type_pos - 1, -1, -1):
            if not (line[i].isalnum() or line[i] in ' ·및관한의에'):
                start = i + 1
                break
        
        # 끝 위치는 법령 타입 뒤
        end = type_pos + len(law_type)
        
        # 시행령/시행규칙 처리
        if end < len(line) - 3:
            next_chars = line[end:end+4]
            if '시행령' in next_chars or '시행규칙' in next_chars:
                space_pos = line.find(' ', end)
                if space_pos != -1:
                    end = space_pos
                else:
                    end = len(line)
        
        return line[start:end].strip()
    
    def _validate_law_name(self, law_name: str) -> bool:
        """법령명 유효성 검증"""
        # 길이 체크
        if len(law_name) < 3 or len(law_name) > 100:
            return False
            
        # 제외 키워드 체크
        if law_name in self.patterns.EXCLUDE_KEYWORDS:
            return False
            
        # 한글 포함 체크
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
            # OpenAI 라이브러리 체크
            try:
                from openai import OpenAI
            except ImportError:
                self.logger.warning("OpenAI 라이브러리가 설치되지 않았습니다.")
                return laws
            
            # API 키 유효성 검증
            if not self.api_key or not self.api_key.startswith('sk-'):
                self.logger.warning("유효하지 않은 OpenAI API 키")
                return laws
            
            client = OpenAI(api_key=self.api_key)
            
            # 텍스트 샘플링 (토큰 제한)
            sample = text[:3000]
            
            # 프롬프트 구성
            prompt = self._create_ai_prompt(sample, laws)
            
            # API 호출 (에러 처리 강화)
            try:
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
                
            except Exception as api_error:
                self.logger.error(f"OpenAI API 호출 오류: {api_error}")
                st.warning("AI 기능을 사용할 수 없습니다. 기본 추출 결과를 사용합니다.")
                return laws
            
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
            st.error(f"Excel 파일 처리 중 오류: {str(e)}")
            
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


# ===== 법령 수집 API 클래스 =====
class LawCollectorAPI:
    """개선된 법령 수집 API 클래스 - 행정규칙 완벽 지원"""
    
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
        from urllib3.util.retry import Retry
        
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
        no_result_laws = []
        
        with ThreadPoolExecutor(max_workers=self.config.MAX_CONCURRENT) as executor:
            # 검색 작업 제출
            future_to_law = {
                executor.submit(self._search_with_variations, law_name): law_name
                for law_name in law_names
            }
            
            # 결과 수집
            for idx, future in enumerate(as_completed(future_to_law)):
                law_name = future_to_law[future]
                
                try:
                    result = future.result()
                    if result:
                        results.extend(result)
                    else:
                        no_result_laws.append(law_name)
                    
                    if progress_callback:
                        progress_callback((idx + 1) / len(law_names))
                        
                except Exception as e:
                    self.logger.error(f"{law_name} 검색 오류: {e}")
                    no_result_laws.append(law_name)
        
        # 검색 실패한 법령 표시
        if no_result_laws:
            with st.expander(f"❌ 검색되지 않은 법령 ({len(no_result_laws)}개)"):
                for law in no_result_laws:
                    st.write(f"- {law}")
                st.info("💡 Tip: 기관코드를 확인하거나, 법령명을 수정해보세요.")
                    
        return results
    
    def _search_with_variations(self, law_name: str) -> List[Dict[str, Any]]:
        """다양한 형식으로 법령 검색"""
        variations = self._generate_search_variations(law_name)
        
        for variation in variations:
            results = self.search_single_law(variation)
            if results:
                # 원래 검색어 저장
                for result in results:
                    result['search_query'] = law_name
                return results
        
        return []
    
    def _generate_search_variations(self, law_name: str) -> List[str]:
        """법령명의 다양한 변형 생성"""
        variations = [law_name]
        
        # 띄어쓰기 추가/제거
        spaced = law_name.replace('및', ' 및 ').replace('에관한', '에 관한')
        if spaced != law_name:
            variations.append(spaced)
        
        no_space = law_name.replace(' ', '')
        if no_space != law_name:
            variations.append(no_space)
        
        # 시행령/시행규칙 분리
        if ' 시행령' in law_name:
            base = law_name.replace(' 시행령', '')
            variations.extend([base, f"{base}시행령"])
        
        if ' 시행규칙' in law_name:
            base = law_name.replace(' 시행규칙', '')
            variations.extend([base, f"{base}시행규칙"])
        
        return variations[:3]  # 최대 3개까지만
    
    def search_single_law(self, law_name: str) -> List[Dict[str, Any]]:
        """단일 법령 검색 - 일반 법령과 행정규칙 모두"""
        results = []
        
        # 일반 법령 검색
        general_laws = self._search_general_law(law_name)
        results.extend(general_laws)
        
        # 행정규칙 키워드 확인
        admin_keywords = LawPatterns.ADMIN_KEYWORDS
        if any(keyword in law_name for keyword in admin_keywords):
            # 행정규칙 검색 (새로운 URL과 파라미터 사용)
            admin_rules = self._search_admin_rule_new(law_name)
            results.extend(admin_rules)
            
            # 행정규칙만 검색된 경우 로그
            if admin_rules and not general_laws:
                self.logger.info(f"'{law_name}'는 행정규칙으로만 검색되었습니다.")
        
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
            laws = self._parse_law_search_response(response.text, law_name)
            return laws
            
        except Exception as e:
            self.logger.error(f"일반 법령 검색 오류: {e}")
            return []
    
    def _search_admin_rule_new(self, law_name: str) -> List[Dict[str, Any]]:
        """행정규칙 검색 - 새로운 API 사용"""
        # 법제처 웹사이트의 실제 파라미터 사용
        params = {
            'OC': self.oc_code,
            'type': 'XML',
            'query': law_name,
            'target': 'admrul',
            'display': '100',
            'page': '1',
            'sort': 'date'
        }
        
        try:
            self.logger.info(f"행정규칙 검색 시작: {law_name}")
            
            # 먼저 일반 법령 검색 API로 시도 (행정규칙도 포함될 수 있음)
            response = self.session.get(
                self.config.LAW_SEARCH_URL,
                params=params,
                timeout=self.config.TIMEOUT
            )
            
            if response.status_code == 200:
                rules = self._parse_admin_rule_from_law_api(response.text, law_name)
                if rules:
                    self.logger.info(f"행정규칙 {len(rules)}개 발견: {law_name}")
                    return rules
            
            # 대체 방법: 행정규칙 전용 검색
            return self._search_admin_rule_alternative(law_name)
            
        except Exception as e:
            self.logger.error(f"행정규칙 검색 오류: {e}")
            return []
    
    def _search_admin_rule_alternative(self, law_name: str) -> List[Dict[str, Any]]:
        """행정규칙 대체 검색 방법"""
        # 법제처 웹사이트처럼 일반 법령 API로 행정규칙 검색
        params = {
            'OC': self.oc_code,
            'target': 'law',  # law로 설정
            'type': 'XML',
            'query': law_name,
            'display': '100',
            'page': '1',
            'LSC': 'admrul'  # 법령구분: 행정규칙
        }
        
        try:
            response = self.session.get(
                self.config.LAW_SEARCH_URL,
                params=params,
                timeout=self.config.TIMEOUT
            )
            
            if response.status_code != 200:
                return []
            
            # 일반 법령 파싱 방식으로 처리하되 행정규칙 플래그 추가
            laws = self._parse_law_search_response(response.text, law_name)
            for law in laws:
                law['is_admin_rule'] = True
            
            return laws
            
        except Exception as e:
            self.logger.error(f"행정규칙 대체 검색 오류: {e}")
            return []
    
    def _parse_admin_rule_from_law_api(self, content: str, 
                                      search_query: str) -> List[Dict[str, Any]]:
        """일반 법령 API 응답에서 행정규칙 파싱"""
        rules = []
        
        try:
            # 전처리
            content = self._preprocess_xml_content(content)
            
            # XML 파싱
            root = ET.fromstring(content.encode('utf-8'))
            
            # 일반 법령 형식으로 파싱하되 행정규칙 여부 확인
            for law_elem in root.findall('.//law'):
                law_type = law_elem.findtext('법종구분', '')
                
                # 행정규칙 타입 확인
                admin_types = ['훈령', '예규', '고시', '규정', '지침', '세칙']
                is_admin = any(admin_type in law_type for admin_type in admin_types)
                
                if is_admin:
                    law_info = {
                        'law_id': law_elem.findtext('법령ID', ''),
                        'law_msn': law_elem.findtext('법령일련번호', ''),
                        'law_name': law_elem.findtext('법령명한글', ''),
                        'law_type': law_type,
                        'promulgation_date': law_elem.findtext('공포일자', ''),
                        'enforcement_date': law_elem.findtext('시행일자', ''),
                        'is_admin_rule': True,
                        'search_query': search_query
                    }
                    
                    if law_info['law_id'] and law_info['law_name']:
                        rules.append(law_info)
                        
        except ET.ParseError as e:
            self.logger.error(f"XML 파싱 오류: {e}")
            
        return rules
    
    def _parse_law_search_response(self, content: str, 
                                  search_query: str) -> List[Dict[str, Any]]:
        """법령 검색 응답 파싱"""
        laws = []
        
        try:
            # 전처리
            content = self._preprocess_xml_content(content)
            
            # XML 파싱
            root = ET.fromstring(content.encode('utf-8'))
            
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
    
    def _preprocess_xml_content(self, content: str) -> str:
        """XML 내용 전처리"""
        # BOM 제거
        if content.startswith('\ufeff'):
            content = content[1:]
        
        # XML 헤더 확인
        if not content.strip().startswith('<?xml'):
            content = '<?xml version="1.0" encoding="UTF-8"?>\n' + content
        
        # 특수문자 제거
        content = re.sub(r'[\x00-\x08\x0B-\x0C\x0E-\x1F\x7F]', '', content)
        
        return content
    
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
                    law.get('is_admin_rule', False)
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
        # 행정규칙도 일반 법령과 동일한 방식으로 처리
        return self._get_general_law_detail(law_id, law_msn, law_name)
    
    def _get_general_law_detail(self, law_id: str, law_msn: str, 
                               law_name: str) -> Optional[Dict[str, Any]]:
        """일반 법령 상세 정보 (행정규칙 포함)"""
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
            return self._parse_law_detail(response.text, law_id, law_msn, law_name)
            
        except Exception as e:
            self.logger.error(f"법령 상세 조회 오류: {e}")
            return None
    
    def _parse_law_detail(self, content: str, law_id: str, 
                         law_msn: str, law_name: str) -> Dict[str, Any]:
        """법령 상세 정보 파싱"""
        detail = {
            'law_id': law_id,
            'law_msn': law_msn,
            'law_name': law_name,
            'law_type': '',
            'promulgation_date': '',
            'enforcement_date': '',
            'department': '',
            'articles': [],
            'supplementary_provisions': [],
            'attachments': [],
            'raw_content': ''
        }
        
        try:
            # 전처리
            content = self._preprocess_xml_content(content)
            
            # XML 파싱
            root = ET.fromstring(content.encode('utf-8'))
            
            # 기본 정보
            basic_info = root.find('.//기본정보')
            if basic_info is not None:
                detail['law_type'] = basic_info.findtext('법종구분명', '')
                detail['department'] = basic_info.findtext('소관부처명', '')
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
                
            self.logger.info(f"상세 정보 파싱 완료: {law_name} - 조문 {len(detail['articles'])}개")
                
        except Exception as e:
            self.logger.error(f"상세 정보 파싱 오류: {e}")
            
        return detail
    
    def _extract_articles(self, root: ET.Element, detail: Dict[str, Any]) -> None:
        """조문 추출"""
        # 표준 조문 구조
        articles_section = root.find('.//조문')
        if articles_section is not None:
            for article_unit in articles_section.findall('.//조문단위'):
                article = self._parse_article_unit(article_unit)
                if article:
                    detail['articles'].append(article)
            return
        
        # 조문내용 직접 찾기
        for article_content in root.findall('.//조문내용'):
            if article_content.text:
                articles = self._parse_article_text(article_content.text)
                detail['articles'].extend(articles)
    
    def _parse_article_unit(self, article_elem: ET.Element) -> Optional[Dict[str, Any]]:
        """조문단위 파싱"""
        article = {
            'number': '',
            'title': '',
            'content': '',
            'paragraphs': []
        }
        
        # 조문번호
        article_num = article_elem.findtext('조문번호', '')
        if article_num:
            article['number'] = f"제{article_num}조"
        
        # 조문제목
        article['title'] = article_elem.findtext('조문제목', '')
        
        # 조문내용
        article['content'] = article_elem.findtext('조문내용', '')
        
        # 항 추출
        for para in article_elem.findall('.//항'):
            paragraph = {
                'number': para.findtext('항번호', ''),
                'content': para.findtext('항내용', '')
            }
            if paragraph['content']:
                article['paragraphs'].append(paragraph)
        
        return article if (article['number'] or article['content']) else None
    
    def _parse_article_text(self, text: str) -> List[Dict[str, Any]]:
        """조문 텍스트 파싱"""
        articles = []
        
        # 조문 패턴
        pattern = r'(제\d+조(?:의\d+)?)\s*(?:\((.*?)\))?\s*(.*?)(?=제\d+조|$)'
        matches = re.findall(pattern, text, re.DOTALL)
        
        for match in matches:
            article = {
                'number': match[0],
                'title': match[1],
                'content': match[2].strip(),
                'paragraphs': []
            }
            articles.append(article)
            
        return articles
    
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
        
        # 부칙내용 직접 찾기
        if not detail['supplementary_provisions']:
            for elem in root.findall('.//부칙내용'):
                if elem.text:
                    detail['supplementary_provisions'].append({
                        'number': '',
                        'promulgation_date': '',
                        'content': elem.text
                    })
    
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


# ===== 법령 내보내기 클래스 =====
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
                'admin_rule_count': sum(1 for law in laws_dict.values() if '행정규칙' in law.get('law_type', '') or law.get('law_type') in ['훈령', '예규', '고시', '규정']),
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
        if law.get('department'):
            lines.append(f"소관부처: {law.get('department', '')}")
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
        
        # 원문 (조문이 없는 경우)
        if not law.get('articles') and law.get('raw_content'):
            lines.append("\n【원 문】\n")
            lines.append(law['raw_content'])
        
        return '\n'.join(lines)
    
    def _format_law_markdown(self, law: Dict[str, Any]) -> str:
        """법령을 Markdown으로 포맷"""
        lines = []
        
        # 제목
        lines.append(f"# {law['law_name']}\n")
        
        # 기본 정보
        lines.append("## 📋 기본 정보\n")
        lines.append(f"- **법종구분**: {law.get('law_type', '')}")
        if law.get('department'):
            lines.append(f"- **소관부처**: {law.get('department', '')}")
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
        lines.append(f"**총 법령 수**: {len(laws_dict)}개")
        
        # 통계
        admin_rule_count = sum(1 for law in laws_dict.values() 
                             if '행정규칙' in law.get('law_type', '') or 
                             law.get('law_type') in ['훈령', '예규', '고시', '규정'])
        if admin_rule_count > 0:
            lines.append(f"**행정규칙 수**: {admin_rule_count}개")
        
        lines.append("")
        
        # 목차
        lines.append("## 📑 목차\n")
        for idx, (law_id, law) in enumerate(laws_dict.items(), 1):
            anchor = self._sanitize_filename(law['law_name'])
            type_emoji = "📋" if (law.get('is_admin_rule') or 
                                 law.get('law_type') in ['훈령', '예규', '고시', '규정']) else "📖"
            lines.append(f"{idx}. {type_emoji} [{law['law_name']}](#{anchor})")
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
        admin_rule_count = sum(1 for law in laws_dict.values() 
                              if '행정규칙' in law.get('law_type', '') or 
                              law.get('law_type') in ['훈령', '예규', '고시', '규정'])
        
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
- 행정규칙 수: {admin_rule_count}개

## 📖 수집된 법령 목록

"""
        
        # 일반 법령과 행정규칙 분리
        general_laws = []
        admin_rules = []
        
        for law_id, law in laws_dict.items():
            if ('행정규칙' in law.get('law_type', '') or 
                law.get('law_type') in ['훈령', '예규', '고시', '규정']):
                admin_rules.append((law_id, law))
            else:
                general_laws.append((law_id, law))
        
        # 일반 법령 목록
        if general_laws:
            content += "\n### 📖 일반 법령\n\n"
            for law_id, law in general_laws:
                content += f"#### {law['law_name']}\n"
                content += f"- 법종구분: {law.get('law_type', '')}\n"
                content += f"- 시행일자: {law.get('enforcement_date', '')}\n"
                content += f"- 조문: {len(law.get('articles', []))}개\n\n"
        
        # 행정규칙 목록
        if admin_rules:
            content += "\n### 📋 행정규칙\n\n"
            for law_id, law in admin_rules:
                content += f"#### {law['law_name']}\n"
                content += f"- 유형: {law.get('law_type', '')}\n"
                if law.get('department'):
                    content += f"- 소관부처: {law.get('department', '')}\n"
                content += f"- 시행일자: {law.get('enforcement_date', '')}\n"
                content += f"- 조문: {len(law.get('articles', []))}개\n\n"
            
        return content


# ===== Streamlit UI 함수들 =====
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
        
        # 기관코드 입력
        oc_code = st.text_input(
            "기관코드 (OC)",
            value=st.session_state.get('oc_code', ''),
            placeholder="이메일 @ 앞부분",
            help="예: test@korea.kr → test"
        )
        st.session_state.oc_code = oc_code
        
        st.divider()
        
        # AI 설정 (사이드바 직접 입력 방식 유지)
        with st.expander("🤖 AI 설정 (선택사항)", expanded=False):
            st.markdown("**ChatGPT를 사용하여 법령명 추출 정확도를 높입니다**")
            
            # OpenAI 라이브러리 설치 확인
            try:
                import openai
                openai_available = True
            except ImportError:
                openai_available = False
                st.warning("⚠️ OpenAI 라이브러리가 설치되지 않았습니다.")
                st.info("설치하려면: `pip install openai`")
            
            if openai_available:
                # 직접 입력 방식
                api_key = st.text_input(
                    "OpenAI API Key",
                    type="password",
                    value=st.session_state.get('openai_api_key', ''),
                    help="https://platform.openai.com/api-keys 에서 발급"
                )
                
                if api_key:
                    # 간단한 유효성 검증
                    if api_key.startswith('sk-') and len(api_key) > 20:
                        st.session_state.openai_api_key = api_key
                        st.session_state.use_ai = True
                        st.success("✅ API 키가 설정되었습니다!")
                    else:
                        st.error("❌ 올바른 형식의 API 키가 아닙니다.")
                        st.session_state.use_ai = False
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
        
        # 테스트 버튼 추가
        st.divider()
        st.subheader("🧪 테스트")
        
        if st.button("행정규칙 검색 테스트", type="secondary", use_container_width=True):
            if not oc_code:
                st.error("기관코드를 먼저 입력해주세요!")
            else:
                test_admin_rule_search(oc_code)
        
        # 초기화 버튼
        if st.button("🔄 초기화", type="secondary", use_container_width=True):
            keys_to_keep = ['mode', 'openai_api_key', 'use_ai', 'oc_code']
            for key in list(st.session_state.keys()):
                if key not in keys_to_keep:
                    del st.session_state[key]
            st.session_state.file_processed = False
            st.rerun()
        
        return oc_code


def test_admin_rule_search(oc_code: str):
    """행정규칙 검색 테스트"""
    with st.spinner("행정규칙 검색 테스트 중..."):
        collector = LawCollectorAPI(oc_code)
        
        # 테스트할 행정규칙들
        test_rules = [
            "금융기관검사및제재에관한규정",
            "여신전문금융업감독규정",
            "여신전문금융업감독업무시행세칙"
        ]
        
        results = []
        for rule_name in test_rules:
            st.write(f"\n🔍 검색 중: {rule_name}")
            
            found = collector.search_single_law(rule_name)
            if found:
                st.success(f"✅ {len(found)}개 발견!")
                for item in found:
                    type_emoji = "📋" if item.get('is_admin_rule') else "📖"
                    st.write(f"  {type_emoji} {item['law_name']} ({item['law_type']})")
            else:
                st.warning(f"❌ 검색 결과 없음")
            
            results.extend(found)
            time.sleep(0.5)
        
        if results:
            st.sidebar.success(f"총 {len(results)}개 법령 발견!")
        else:
            st.sidebar.error("법령을 찾을 수 없습니다.")


def handle_direct_search_mode(oc_code: str):
    """직접 검색 모드 처리"""
    st.header("🔍 직접 검색 모드")
    
    law_name = st.text_input(
        "법령명",
        placeholder="예: 민법, 상법, 금융감독규정",
        help="검색할 법령명을 입력하세요 (행정규칙도 검색 가능)"
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
                    
                    # 행정규칙 개수 표시
                    admin_count = sum(1 for r in results if r.get('is_admin_rule'))
                    if admin_count > 0:
                        st.info(f"📋 이 중 {admin_count}개는 행정규칙입니다.")
                    
                    st.session_state.search_results = results
                else:
                    st.warning("검색 결과가 없습니다.")
                    st.info("💡 Tip: 띄어쓰기를 조정하거나 기관코드를 확인해보세요.")
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
    col1, col2 = st.columns([3, 1])
    with col1:
        for idx, law in enumerate(st.session_state.extracted_laws, 1):
            # 행정규칙 키워드 체크
            is_admin = any(k in law for k in LawPatterns.ADMIN_KEYWORDS)
            emoji = "📋" if is_admin else "📖"
            st.write(f"{idx}. {emoji} {law}")
    
    with col2:
        st.metric("총 법령", len(st.session_state.extracted_laws))
        admin_count = sum(1 for law in st.session_state.extracted_laws 
                         if any(k in law for k in LawPatterns.ADMIN_KEYWORDS))
        if admin_count > 0:
            st.metric("행정규칙", admin_count)
    
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
        
        # 행정규칙 통계
        admin_count = sum(1 for r in results if r.get('is_admin_rule'))
        if admin_count > 0:
            st.info(f"📋 이 중 {admin_count}개는 행정규칙입니다.")
        
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
    cols = st.columns([1, 1, 3, 2, 2, 2])
    headers = ["선택", "유형", "법령명", "법종구분", "시행일자", "검색어"]
    for col, header in zip(cols, headers):
        col.markdown(f"**{header}**")
    
    st.divider()
    
    # 결과 표시
    selected_indices = []
    for idx, law in enumerate(st.session_state.search_results):
        cols = st.columns([1, 1, 3, 2, 2, 2])
        
        with cols[0]:
            # 빈 레이블 경고 해결: label_visibility 사용
            if st.checkbox("선택", key=f"sel_{idx}", value=select_all, label_visibility="collapsed"):
                selected_indices.append(idx)
        
        with cols[1]:
            # 유형 아이콘
            if law.get('is_admin_rule') or law.get('law_type') in ['훈령', '예규', '고시', '규정']:
                st.write("📋")  # 행정규칙
            else:
                st.write("📖")  # 일반 법령
        
        with cols[2]:
            st.write(law['law_name'])
        
        with cols[3]:
            st.write(law.get('law_type', ''))
        
        with cols[4]:
            st.write(law.get('enforcement_date', ''))
        
        with cols[5]:
            st.write(law.get('search_query', ''))
    
    # 선택된 법령 저장
    st.session_state.selected_laws = [
        st.session_state.search_results[i] for i in selected_indices
    ]
    
    if st.session_state.selected_laws:
        st.success(f"{len(st.session_state.selected_laws)}개 법령이 선택되었습니다")
        
        # 선택된 행정규칙 개수
        selected_admin = sum(1 for law in st.session_state.selected_laws 
                           if law.get('is_admin_rule') or 
                           law.get('law_type') in ['훈령', '예규', '고시', '규정'])
        if selected_admin > 0:
            st.info(f"📋 선택된 행정규칙: {selected_admin}개")
        
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
    
    success_count = len(collected)
    total_count = len(st.session_state.selected_laws)
    status_text.text(f"수집 완료! (성공: {success_count}/{total_count})")
    
    if success_count < total_count:
        failed_laws = [law['law_name'] for law in st.session_state.selected_laws 
                      if law['law_id'] not in collected]
        with st.expander("❌ 수집 실패한 법령"):
            for law_name in failed_laws:
                st.write(f"- {law_name}")
    
    st.session_state.collected_laws = collected
    
    # 통계 표시
    display_collection_stats(collected)


def display_collection_stats(collected_laws: Dict[str, Dict[str, Any]]):
    """수집 통계 표시"""
    total_articles = sum(len(law.get('articles', [])) for law in collected_laws.values())
    total_provisions = sum(len(law.get('supplementary_provisions', [])) for law in collected_laws.values())
    total_attachments = sum(len(law.get('attachments', [])) for law in collected_laws.values())
    admin_rule_count = sum(1 for law in collected_laws.values() 
                          if '행정규칙' in law.get('law_type', '') or 
                          law.get('law_type') in ['훈령', '예규', '고시', '규정'])
    
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("총 조문", f"{total_articles:,}개")
    with col2:
        st.metric("총 부칙", f"{total_provisions}개")
    with col3:
        st.metric("총 별표/별첨", f"{total_attachments}개")
    with col4:
        st.metric("행정규칙", f"{admin_rule_count}개")


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
            emoji = "📋" if ('행정규칙' in law.get('law_type', '') or 
                           law.get('law_type') in ['훈령', '예규', '고시', '규정']) else "📖"
            st.subheader(f"{emoji} {law['law_name']}")
            
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
    st.markdown("법제처 Open API를 활용한 법령 수집 도구 (v6.1)")
    st.markdown("**✨ 행정규칙(규정, 고시, 훈령 등) 완벽 지원!**")
    
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
