"""
법제처 법령 수집기 - 다양한 법률 데이터 지원 버전 (v7.0)
- 자치법규, 판례, 헌재결정례, 법령해석례, 행정심판례, 조약 검색 지원
- PDF 다운로드 로직 제거, OCR 텍스트 추출 기능 추가
- 초기화 시 기관코드/API키 유지
- 별표/별첨 텍스트 내용 자동 수집
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
from typing import List, Set, Dict, Optional, Tuple, Any, cast
from dataclasses import dataclass
from functools import lru_cache
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import base64
import urllib.parse
from collections import defaultdict, deque

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
    # 법제처 API 엔드포인트
    LAW_SEARCH_URL = "https://www.law.go.kr/DRF/lawSearch.do"  # 일반 법령 검색
    LAW_DETAIL_URL = "https://www.law.go.kr/DRF/lawService.do"  # 법령 상세
    ADMIN_RULE_SEARCH_URL = "https://www.law.go.kr/DRF/lawSearch.do"  # 행정규칙 검색
    ADMIN_RULE_DETAIL_URL = "https://www.law.go.kr/DRF/lawService.do"  # 행정규칙도 동일 서비스 사용
    
    # API 설정
    DEFAULT_DELAY = 0.3  # API 호출 간격 (초)
    MAX_RETRIES = 3      # 최대 재시도 횟수
    TIMEOUT = 30         # 타임아웃 (초)
    MAX_CONCURRENT = 5   # 최대 동시 요청 수
    
    # 페이지당 결과 수
    RESULTS_PER_PAGE = 100


class LawPatterns:
    """법령명 추출 패턴을 관리하는 클래스 - 개선된 버전"""
    
    # 제외 키워드 (수정: 키워드만 남김)
    EXCLUDE_KEYWORDS = {
        '상하위법', '관련법령', '상위법', '하위법', '선택된'
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
    
    # 제거할 접두어 패턴
    PREFIX_PATTERNS = [
        r'^행정규칙\s*',
        r'^법령\s*',
        r'^\d{8}\s*',  # 날짜 형식 (20250422 같은)
        r'^\d+\.\s*',  # 번호 형식 (1. 2. 같은)
    ]
    
    # 법령명 패턴 (정규표현식) - 개선된 버전
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
        # 고시/훈령 패턴 추가
        r'([가-힣]+(?:\s+[가-힣]+)*(?:고시|훈령|예규|지침))(?:\s|$)',
        # 분류 패턴 추가
        r'([가-힣]+(?:\s+)?분류)(?:\s|$)',
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
        """텍스트에서 법령명 추출 - 개선된 버전"""
        laws = set()
        
        # 텍스트 정규화
        text = self._normalize_text(text)
        
        # 제외 키워드로 텍스트 분할 처리
        # '여신전문금융업법 상하위법 여신전문금융업법' -> 분리
        for exclude_keyword in self.patterns.EXCLUDE_KEYWORDS:
            text = text.replace(exclude_keyword, '\n')
        
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
            
            # 제외 키워드가 포함된 라인은 분할 처리
            contains_exclude = False
            for exclude_keyword in self.patterns.EXCLUDE_KEYWORDS:
                if exclude_keyword in line:
                    # 제외 키워드 앞뒤로 분할
                    parts = line.split(exclude_keyword)
                    for part in parts:
                        part = part.strip()
                        if part and part not in self.patterns.EXCLUDE_KEYWORDS:
                            # 각 부분에서 법령명 추출
                            for law_type in self.patterns.LAW_TYPES:
                                if law_type in part:
                                    law_name = self._extract_law_name_from_line(part, law_type)
                                    if law_name and self._validate_law_name(law_name):
                                        laws.add(law_name)
                    contains_exclude = True
                    break
            
            if contains_exclude:
                continue
                
            # 접두어 제거
            for prefix_pattern in self.patterns.PREFIX_PATTERNS:
                line = re.sub(prefix_pattern, '', line)
            
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
        
        # 접두어 제거
        for prefix_pattern in self.patterns.PREFIX_PATTERNS:
            law_name = re.sub(prefix_pattern, '', law_name)
        
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
            
        # 접두어가 남아있는 경우 제거
        if any(pattern in law_name for pattern in ['행정규칙', '법령']):
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
                
                # 접두어 최종 제거
                for prefix_pattern in self.patterns.PREFIX_PATTERNS:
                    law = re.sub(prefix_pattern, '', law)
                
                processed.add(law)
                
        return processed
    
    def _enhance_with_ai(self, text: str, laws: Set[str]) -> Set[str]:
        """AI를 활용한 법령명 추출 개선 - 강화된 버전"""
        try:
            # OpenAI 라이브러리 체크
            try:
                from openai import OpenAI
            except ImportError:
                self.logger.warning("OpenAI 라이브러리가 설치되지 않았습니다.")
                return laws
            
            # API 키 유효성 검증 개선
            if not self.api_key:
                self.logger.warning("API 키가 설정되지 않았습니다.")
                return laws
            
            # API 키 정리 (공백 제거, 특수문자 확인)
            cleaned_key = self.api_key.strip()
            
            # API 키 형식 검증
            if not (cleaned_key.startswith('sk-') or cleaned_key.startswith('sess-')):
                self.logger.warning(f"유효하지 않은 API 키 형식")
                return laws
            
            self.logger.info(f"OpenAI API 키 사용 중: {cleaned_key[:10]}...")
            
            # OpenAI 클라이언트 생성 (키 재설정)
            client = OpenAI(
                api_key=cleaned_key,
                max_retries=2,
                timeout=30.0
            )
            
            # API 키 테스트를 위한 간단한 호출
            try:
                # 텍스트 샘플링 (토큰 제한)
                sample = text[:3000]
                
                # 프롬프트 구성 - 강화된 버전
                prompt = self._create_enhanced_ai_prompt(sample, laws, text)
                
                # API 호출 - 안전한 방식으로
                try:
                    response = client.chat.completions.create(
                        model="gpt-5",
                        messages=[
                            {"role": "system", "content": "한국 법령 전문가. 법령체계도에서 법령명을 정확히 추출하고, 특수문자 변환과 사용자 의도를 파악합니다."},
                            {"role": "user", "content": prompt}
                        ],
                        temperature=0.1,
                        max_tokens=1500
                    )

                    # 응답 파싱
                    ai_laws = self._parse_ai_response_enhanced(response.choices[0].message.content)

                    self.logger.info(f"AI가 추가로 {len(ai_laws - laws)}개의 법령을 찾았습니다.")

                    # 결과 병합
                    return laws.union(ai_laws)

                except Exception as chat_error:
                    # gpt-5 실패 시 재시도
                    try:
                        response = client.chat.completions.create(
                            model="gpt-5",
                            messages=[
                                {"role": "system", "content": "한국 법령 전문가. 법령체계도에서 법령명을 정확히 추출하고, 특수문자 변환과 사용자 의도를 파악합니다."},
                                {"role": "user", "content": prompt}
                            ],
                            temperature=0.1,
                            max_tokens=1500
                        )

                        ai_laws = self._parse_ai_response_enhanced(response.choices[0].message.content)
                        self.logger.info(f"GPT-5로 {len(ai_laws - laws)}개의 법령을 추가로 찾았습니다.")
                        return laws.union(ai_laws)

                    except:
                        self.logger.warning("AI 기능을 사용할 수 없습니다. 기본 추출만 수행합니다.")
                        return laws
                
            except Exception as api_error:
                error_msg = str(api_error)
                if "401" in error_msg or "Incorrect API key" in error_msg:
                    self.logger.error("API 키가 유효하지 않습니다. 키를 다시 확인해주세요.")
                    st.error("⚠️ OpenAI API 키가 유효하지 않습니다. 올바른 키인지 확인해주세요.")
                elif "insufficient_quota" in error_msg:
                    self.logger.warning("API 사용량 한도 초과")
                    st.warning("⚠️ OpenAI API 사용량 한도를 초과했습니다.")
                else:
                    self.logger.error(f"OpenAI API 호출 오류: {error_msg}")
                return laws
            
        except Exception as e:
            self.logger.error(f"AI 처리 오류: {e}")
            return laws
    
    def _create_enhanced_ai_prompt(self, sample: str, existing_laws: Set[str], full_text: str) -> str:
        """강화된 AI 프롬프트 생성"""
        # 문서 구조 분석
        doc_structure = self._analyze_document_structure(full_text)
        
        return f"""당신은 한국 법령 전문가입니다. 다음 법령체계도 문서에서 법령명을 정확히 추출하세요.

중요 규칙:
1. 법제처 공식 명칭 사용
2. 특수문자 변환: * → ·, ＊ → ·
3. "상하위법", "관련법령", "행정규칙", "법령" 같은 카테고리 제목은 제외
4. 날짜(예: 20250422, [시행 2022.12.11.]) 제외
5. 시행령/시행규칙은 독립된 법령으로 추출
6. 문서에 있는 모든 법령명을 빠짐없이 추출

문서 구조 정보:
{doc_structure}

텍스트 샘플:
{sample}

현재까지 추출된 법령 (참고):
{', '.join(list(existing_laws)[:10])}

다음과 같은 법령들을 특히 주의해서 찾으세요:
- 행정규칙 (규정, 훈령, 예규, 지침, 세칙 등)
- 특수문자가 포함된 법령명 (예: 심의·징계위원회)
- 긴 법령명 (예: 근로기준법 및 공인노무사법에 따른 과태료의 가중처분에 관한 세부 지침)

법령명만 한 줄에 하나씩 출력하세요:"""
    
    def _analyze_document_structure(self, text: str) -> str:
        """문서 구조 분석"""
        lines = text.split('\n')
        structure_info = []
        
        # 카테고리 키워드
        category_keywords = ['상하위법', '관련법령', '행정규칙', '법령']
        
        current_category = None
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # 카테고리 감지
            for keyword in category_keywords:
                if keyword in line and len(line) < 20:  # 짧은 라인에서만
                    current_category = keyword
                    structure_info.append(f"[{keyword} 섹션 시작]")
                    break
            
            # 날짜 패턴 감지
            if re.search(r'\[시행\s*\d{4}\.\s*\d{1,2}\.\s*\d{1,2}\.\]', line):
                structure_info.append(f"날짜가 포함된 법령 발견: {line[:50]}...")
        
        return '\n'.join(structure_info[:10])  # 최대 10개까지만
    
    def _parse_ai_response_enhanced(self, response: str) -> Set[str]:
        """강화된 AI 응답 파싱"""
        laws = set()
        
        for line in response.strip().split('\n'):
            line = line.strip()
            
            # 번호, 기호 제거
            line = re.sub(r'^[\d\-\.\*\•\·]+\s*', '', line)
            line = line.strip('"\'')
            
            # 접두어 제거
            for prefix_pattern in self.patterns.PREFIX_PATTERNS:
                line = re.sub(prefix_pattern, '', line)
            
            # 특수문자 정규화
            line = self._normalize_law_name_for_ai(line)
            
            if line and self._validate_law_name(line):
                laws.add(line)
                self.logger.debug(f"AI 추출: {line}")
                
        return laws
    
    def _normalize_law_name_for_ai(self, law_name: str) -> str:
        """AI 응답에서 법령명 정규화"""
        # 특수문자 변환
        replacements = {
            '*': '·',
            '＊': '·',
            '․': '·',
            '･': '·',
            '・': '·',
            '，': ',',
            '．': '.',
            '（': '(',
            '）': ')',
        }
        
        for old, new in replacements.items():
            law_name = law_name.replace(old, new)
        
        # 연속 공백 제거
        law_name = ' '.join(law_name.split())
        
        return law_name.strip()
    
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
    """개선된 법령 수집 API 클래스 - 정확한 검색 모드 추가"""
    
    def __init__(self, oc_code: str):
        self.oc_code = oc_code
        self.config = APIConfig()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.session = self._create_session()
        self._cache = {}  # 검색 결과 캐시
        self.patterns = LawPatterns()
        
    @lru_cache(maxsize=128)
    def _get_cached_search_result(self, law_name: str) -> Optional[str]:
        """캐시된 검색 결과 반환"""
        return None  # 실제 구현시 캐시 로직 추가
        
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
                   progress_callback=None, 
                   use_variations: bool = True) -> List[Dict[str, Any]]:
        """여러 법령을 병렬로 검색 - 중복 제거 추가
        
        Args:
            law_names: 검색할 법령명 리스트
            progress_callback: 진행률 콜백 함수
            use_variations: 법령명 변형 사용 여부 (기본값: True)
                           - True: 띄어쓰기 등 변형하여 검색 (직접 검색 모드)
                           - False: 정확한 법령명으로만 검색 (법령체계도 모드)
        """
        results = []
        no_result_laws = []
        seen_law_ids = set()  # 중복 제거를 위한 set
        
        with ThreadPoolExecutor(max_workers=self.config.MAX_CONCURRENT) as executor:
            # 검색 작업 제출
            if use_variations:
                # 변형 검색 사용 (직접 검색 모드)
                future_to_law = {
                    executor.submit(self._search_with_variations, law_name): law_name
                    for law_name in law_names
                }
            else:
                # 정확한 검색만 사용 (법령체계도 모드)
                future_to_law = {
                    executor.submit(self._search_exact_match, law_name): law_name
                    for law_name in law_names
                }
            
            # 결과 수집
            for idx, future in enumerate(as_completed(future_to_law)):
                law_name = future_to_law[future]
                
                try:
                    result = future.result()
                    if result:
                        # 결과가 리스트가 아닌 경우 리스트로 변환
                        if not isinstance(result, list):
                            result = [result] if result else []
                        
                        # 중복 제거
                        for law in result:
                            if law['law_id'] not in seen_law_ids:
                                seen_law_ids.add(law['law_id'])
                                # 원래 검색어 저장
                                law['search_query'] = law_name
                                results.append(law)
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
                
                # 모드에 따른 다른 안내 메시지
                if use_variations:
                    st.info("💡 Tip: 기관코드를 확인하거나, 법령명을 수정해보세요.")
                else:
                    st.info("💡 Tip: 법령체계도의 법령명과 정확히 일치하는 법령만 검색됩니다.")
                    
        return results
    
    def _search_exact_match(self, law_name: str) -> List[Dict[str, Any]]:
        """개선된 매칭으로 법령 검색 - 파일 업로드 모드용"""
        self.logger.info(f"파일 업로드 검색 모드: {law_name}")
        
        # 특수문자 정규화
        normalized_name = self._normalize_law_name(law_name)
        
        # 기본 검색 + 정규화된 이름으로도 검색
        all_results = []
        
        # 1. 원본 그대로 검색
        results = self._search_single_law_exact(law_name)
        all_results.extend(results)
        
        # 2. 정규화된 이름으로 검색 (다른 경우만)
        if normalized_name != law_name:
            normalized_results = self._search_single_law_exact(normalized_name)
            all_results.extend(normalized_results)
        
        # 중복 제거
        seen_ids = set()
        unique_results = []
        
        for result in all_results:
            if result['law_id'] not in seen_ids:
                seen_ids.add(result['law_id'])
                unique_results.append(result)
        
        # 결과 필터링 - 유사도 기반
        filtered_results = []
        for result in unique_results:
            similarity = self._calculate_similarity(law_name, result['law_name'])
            if similarity >= 0.85:  # 85% 이상 유사도
                filtered_results.append(result)
                self.logger.debug(f"매칭 성공 (유사도 {similarity:.2f}): {result['law_name']}")
            else:
                self.logger.debug(f"매칭 실패 (유사도 {similarity:.2f}): {result['law_name']} != {law_name}")
        
        return filtered_results
    
    def _normalize_law_name(self, law_name: str) -> str:
        """법령명 정규화 - 특수문자 처리"""
        normalized = law_name
        
        # 특수문자 변환
        replacements = {
            '*': '·',
            '＊': '·',
            '․': '·',
            '･': '·',
            '・': '·',
            '，': ',',
            '．': '.',
            '（': '(',
            '）': ')',
            '「': '',
            '」': '',
            '『': '',
            '』': '',
        }
        
        for old, new in replacements.items():
            normalized = normalized.replace(old, new)
        
        # 연속 공백 제거
        normalized = ' '.join(normalized.split())
        
        return normalized.strip()
    
    def _calculate_similarity(self, str1: str, str2: str) -> float:
        """두 문자열의 유사도 계산 (0~1)"""
        # 간단한 문자 기반 유사도
        str1 = self._normalize_law_name(str1.lower())
        str2 = self._normalize_law_name(str2.lower())
        
        if str1 == str2:
            return 1.0
        
        # 레벤슈타인 거리 기반 유사도
        longer = max(len(str1), len(str2))
        if longer == 0:
            return 1.0
        
        distance = self._levenshtein_distance(str1, str2)
        return (longer - distance) / longer
    
    def _levenshtein_distance(self, s1: str, s2: str) -> int:
        """레벤슈타인 거리 계산"""
        if len(s1) < len(s2):
            return self._levenshtein_distance(s2, s1)
        
        if len(s2) == 0:
            return len(s1)
        
        previous_row = range(len(s2) + 1)
        for i, c1 in enumerate(s1):
            current_row = [i + 1]
            for j, c2 in enumerate(s2):
                insertions = previous_row[j + 1] + 1
                deletions = current_row[j] + 1
                substitutions = previous_row[j] + (c1 != c2)
                current_row.append(min(insertions, deletions, substitutions))
            previous_row = current_row
        
        return previous_row[-1]
    
    def _search_single_law_exact(self, law_name: str) -> List[Dict[str, Any]]:
        """단일 법령 정확한 검색 - 일반 법령과 행정규칙 모두"""
        results = []
        
        # 1. 일반 법령 검색 (target=law)
        general_laws = self._search_general_law(law_name)
        results.extend(general_laws)
        
        # 2. 행정규칙 검색 (별도 API)
        admin_rules = self._search_admin_rule(law_name)
        results.extend(admin_rules)
        
        # 중복 제거
        unique_results = self._remove_duplicates(results)
        
        # 검색 결과 로그
        if unique_results:
            general_count = sum(1 for r in unique_results if not r.get('is_admin_rule'))
            admin_count = sum(1 for r in unique_results if r.get('is_admin_rule'))
            self.logger.info(f"✅ 정확한 검색 완료: {law_name} - 일반법령 {general_count}개, 행정규칙 {admin_count}개")
        
        return unique_results
    
    def _search_with_variations(self, law_name: str) -> List[Dict[str, Any]]:
        """다양한 형식으로 법령 검색 - 개선된 버전"""
        variations = self._generate_search_variations(law_name)
        all_results = []
        seen_law_ids = set()
        
        for idx, variation in enumerate(variations):
            self.logger.info(f"검색 변형 {idx+1}/{len(variations)}: {variation}")
            results = self.search_single_law(variation)
            
            if results:
                # 어떤 변형으로 찾았는지 기록
                for result in results:
                    # 중복 제거
                    if result['law_id'] not in seen_law_ids:
                        seen_law_ids.add(result['law_id'])
                        result['found_with_variation'] = variation
                        result['variation_index'] = idx
                        result['search_query'] = law_name  # 원래 검색어 보존
                        all_results.append(result)
        
        return all_results
    
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
        
        # 1. 일반 법령 검색 (target=law)
        general_laws = self._search_general_law(law_name)
        results.extend(general_laws)
        
        # 2. 행정규칙 검색 (별도 API)
        admin_rules = self._search_admin_rule(law_name)
        results.extend(admin_rules)
        
        # 중복 제거
        unique_results = self._remove_duplicates(results)
        
        # 검색 결과 로그
        if unique_results:
            general_count = sum(1 for r in unique_results if not r.get('is_admin_rule'))
            admin_count = sum(1 for r in unique_results if r.get('is_admin_rule'))
            self.logger.info(f"✅ 검색 완료: {law_name} - 일반법령 {general_count}개, 행정규칙 {admin_count}개")
        
        return unique_results
    
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
            self.logger.debug(f"일반 법령 검색: {law_name}")
            
            response = self.session.get(
                self.config.LAW_SEARCH_URL,
                params=params,
                timeout=self.config.TIMEOUT
            )
            
            if response.status_code != 200:
                self.logger.warning(f"일반 법령 검색 실패: {law_name} - 상태코드: {response.status_code}")
                return []
                
            # XML 파싱
            laws = self._parse_law_search_response(response.text, law_name)
            
            if laws:
                self.logger.info(f"일반 법령 {len(laws)}개 발견: {law_name}")
            
            return laws
            
        except Exception as e:
            self.logger.error(f"일반 법령 검색 오류: {e}")
            return []
    
    def _search_admin_rule(self, law_name: str) -> List[Dict[str, Any]]:
        """행정규칙 검색 - 완전 재작성"""
        params = {
            'OC': self.oc_code,
            'target': 'admrul',
            'type': 'XML',
            'query': law_name,
            'display': '100',
            'page': '1'
        }
        
        try:
            self.logger.info(f"행정규칙 검색 시작: {law_name}")
            self.logger.debug(f"API URL: {self.config.ADMIN_RULE_SEARCH_URL}")
            self.logger.debug(f"파라미터: {params}")
            
            # 행정규칙 전용 API 사용
            response = self.session.get(
                self.config.ADMIN_RULE_SEARCH_URL,  # admRulSc.do
                params=params,
                timeout=self.config.TIMEOUT
            )
            
            self.logger.debug(f"응답 상태코드: {response.status_code}")
            
            if response.status_code == 200:
                # 행정규칙 전용 파싱
                rules = self._parse_admin_rule_search_response(response.text, law_name)
                
                if rules:
                    self.logger.info(f"✅ 행정규칙 {len(rules)}개 발견: {law_name}")
                else:
                    self.logger.info(f"행정규칙 검색 결과 없음: {law_name}")
                
                return rules
            else:
                self.logger.warning(f"행정규칙 검색 실패: {law_name} - 상태코드: {response.status_code}")
                return []
                
        except Exception as e:
            self.logger.error(f"행정규칙 검색 오류: {e}")
            return []
    
    def _parse_law_search_response(self, content: str, 
                                  search_query: str) -> List[Dict[str, Any]]:
        """일반 법령 검색 응답 파싱"""
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
            self.logger.error(f"일반 법령 XML 파싱 오류: {e}")
            
        return laws
    
    def _parse_admin_rule_search_response(self, content: str, 
                                         search_query: str) -> List[Dict[str, Any]]:
        """행정규칙 검색 응답 파싱 - 전용 파서"""
        rules = []
        
        try:
            # 전처리
            content = self._preprocess_xml_content(content)
            
            # XML 파싱
            root = ET.fromstring(content.encode('utf-8'))
            
            self.logger.debug(f"행정규칙 XML 루트 태그: {root.tag}")
            self.logger.debug(f"하위 요소: {[child.tag for child in root][:5]}")
            
            # 행정규칙은 admrul 태그 사용
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
                    self.logger.debug(f"행정규칙 발견: {rule_info['law_name']}")
                    
        except ET.ParseError as e:
            self.logger.error(f"행정규칙 XML 파싱 오류: {e}")
            self.logger.debug(f"파싱 실패한 내용 일부: {content[:500]}")
            
        return rules
    
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
                           progress_callback=None,
                           expand_hierarchy: bool = False) -> Dict[str, Dict[str, Any]]:
        """법령 상세 정보 병렬 수집 및 선택 시 계층 확장 - 다양한 데이터 유형 지원"""
        collected: Dict[str, Dict[str, Any]] = {}

        with ThreadPoolExecutor(max_workers=self.config.MAX_CONCURRENT) as executor:
            # 수집 작업 제출 - 데이터 유형에 따라 다른 메서드 호출
            future_to_law = {}
            for law in laws:
                data_type = law.get('data_type', '')

                # 새로운 데이터 유형인 경우 get_detail_by_type 사용
                if data_type in ['ordinance', 'precedent', 'constitutional',
                                'interpretation', 'admin_decision', 'treaty']:
                    future = executor.submit(self.get_detail_by_type, law)
                else:
                    # 기존 법령/행정규칙
                    future = executor.submit(
                        self._get_law_detail,
                        law['law_id'],
                        law.get('law_msn', ''),
                        law['law_name'],
                        law.get('is_admin_rule', False)
                    )
                future_to_law[future] = law

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

        if expand_hierarchy and collected:
            self._expand_related_laws(collected)

        return collected
    
    def _expand_related_laws(self, collected: Dict[str, Dict[str, Any]],
                             max_depth: int = 2) -> None:
        """선택된 법령의 관계를 추적하여 시행령·시행규칙·행정규칙을 자동 확장"""
        processed_ids = set(collected.keys())
        seen_candidates: Set[Tuple[str, str]] = set()
        queue: deque[Tuple[str, int]] = deque((law_id, 0) for law_id in collected.keys())

        while queue:
            current_id, depth = queue.popleft()
            current_detail = collected.get(current_id)
            if not current_detail:
                continue

            if depth >= max_depth:
                continue

            candidates = self._generate_hierarchy_candidates(current_detail)
            for related_name in current_detail.get('related_law_names', []) or []:
                candidates.append(("관련 법령", related_name))

            for relation, candidate_name in candidates:
                normalized_candidate = self._normalize_law_name(candidate_name)
                if not normalized_candidate:
                    continue

                candidate_key = (relation, normalized_candidate)
                if candidate_key in seen_candidates:
                    continue
                seen_candidates.add(candidate_key)

                search_results = self._search_exact_match(candidate_name)
                for result in search_results:
                    result_id = result.get('law_id')
                    result_msn = result.get('law_msn')

                    if not result_id or result_id in processed_ids:
                        continue

                    detail = self._get_law_detail(
                        result_id,
                        result_msn,
                        result.get('law_name', candidate_name),
                        result.get('is_admin_rule', False)
                    )

                    if not detail:
                        continue

                    detail.setdefault('related_laws', [])
                    detail['parent_law_id'] = current_id
                    detail['relationship_from_parent'] = relation
                    detail['source_candidate'] = candidate_name

                    collected[result_id] = detail
                    processed_ids.add(result_id)
                    queue.append((result_id, depth + 1))

                    current_detail.setdefault('related_laws', [])
                    current_detail['related_laws'].append({
                        'law_id': result_id,
                        'law_name': detail['law_name'],
                        'relationship': relation,
                        'is_admin_rule': detail.get('is_admin_rule', False)
                    })

    def _generate_hierarchy_candidates(self, law_detail: Dict[str, Any]) -> List[Tuple[str, str]]:
        """법령명을 바탕으로 시행령·시행규칙·행정규칙 후보 생성"""
        law_name = law_detail.get('law_name', '').strip()
        if not law_name:
            return []

        normalized_name = self._normalize_law_name(law_name)
        candidates: List[Tuple[str, str]] = []
        admin_base = self._prepare_admin_base(normalized_name)

        def add_candidate(relation: str, candidate: str) -> None:
            cleaned = self._normalize_law_name(candidate)
            if cleaned and cleaned != normalized_name:
                candidates.append((relation, cleaned))

        if '시행령' in normalized_name:
            base_name = normalized_name.replace(' 시행령', '').replace('시행령', '').strip()
            if base_name:
                add_candidate('모법', base_name)
                add_candidate('시행규칙', f"{base_name} 시행규칙")
                add_candidate('시행세칙', f"{base_name} 시행세칙")
                self._add_admin_candidates(candidates, base_name)
        elif any(suffix in normalized_name for suffix in ['시행규칙', '시행세칙']):
            base_name = normalized_name
            base_name = base_name.replace(' 시행규칙', '').replace('시행규칙', '')
            base_name = base_name.replace(' 시행세칙', '').replace('시행세칙', '').strip()
            if base_name:
                add_candidate('모법', base_name)
                add_candidate('시행령', f"{base_name} 시행령")
                self._add_admin_candidates(candidates, base_name)
        else:
            add_candidate('시행령', f"{normalized_name} 시행령")
            add_candidate('시행규칙', f"{normalized_name} 시행규칙")
            add_candidate('시행세칙', f"{normalized_name} 시행세칙")
            self._add_admin_candidates(candidates, normalized_name)

        if admin_base and admin_base != normalized_name:
            self._add_admin_candidates(candidates, admin_base)

        # 중복 제거
        unique_candidates: Dict[Tuple[str, str], Tuple[str, str]] = {}
        for relation, candidate in candidates:
            key = (relation, candidate)
            unique_candidates[key] = (relation, candidate)

        return list(unique_candidates.values())

    def _add_admin_candidates(self, bucket: List[Tuple[str, str]], base_name: str) -> None:
        """행정규칙 가능성이 높은 후보를 버킷에 추가"""
        admin_base = self._prepare_admin_base(base_name)
        if not admin_base:
            return

        suffixes = [
            '감독규정',
            '감독업무시행세칙',
            '업무시행세칙',
            '감독규정 시행세칙',
            '감독규정시행세칙',
            '규정',
            '고시',
            '훈령',
            '예규',
            '지침'
        ]

        for suffix in suffixes:
            candidate = f"{admin_base}{suffix}".strip()
            if candidate and len(candidate) >= 3:
                normalized_candidate = self._normalize_law_name(candidate)
                if normalized_candidate:
                    bucket.append(('행정규칙', normalized_candidate))

    def _prepare_admin_base(self, base_name: str) -> str:
        """행정규칙용 기본 명칭 생성"""
        admin_base = base_name.strip()
        for suffix in [' 법률', '법률', ' 법', '법']:
            if admin_base.endswith(suffix):
                admin_base = admin_base[:-len(suffix)]
                break
        return admin_base.strip()

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
            return self._parse_law_detail(response.text, law_id, law_msn, law_name)
            
        except Exception as e:
            self.logger.error(f"법령 상세 조회 오류: {e}")
            return None
    
    def _get_admin_rule_detail(self, law_id: str, law_msn: str,
                               law_name: str) -> Optional[Dict[str, Any]]:
        """행정규칙 상세 정보 - ID 파라미터 사용"""
        params = {
            'OC': self.oc_code,
            'target': 'admrul',
            'type': 'XML',
            'ID': law_msn  # MST가 아닌 ID 사용!
        }

        try:
            self.logger.debug(f"행정규칙 상세 조회: {law_name}")
            self.logger.debug(f"파라미터: {params}")
            
            response = self.session.get(
                self.config.ADMIN_RULE_DETAIL_URL,
                params=params,
                timeout=self.config.TIMEOUT
            )
            
            if response.status_code != 200:
                self.logger.warning(f"행정규칙 상세 조회 실패: {response.status_code}")
                return None
                
            # 행정규칙 상세 파싱
            return self._parse_admin_rule_detail(response.text, law_id, law_msn, law_name)
            
        except Exception as e:
            self.logger.error(f"행정규칙 상세 조회 오류: {e}")
            return None
    
    def _parse_law_detail(self, content: str, law_id: str, 
                         law_msn: str, law_name: str) -> Dict[str, Any]:
        """일반 법령 상세 정보 파싱 - 개선된 PDF 추출"""
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
            'attachment_pdfs': [],  # PDF 첨부파일 추가
            'raw_content': '',
            'is_admin_rule': False,
            'related_laws': [],
            'related_law_names': []
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

            # PDF 첨부파일 추출 - 개선된 버전
            self._extract_pdf_attachments_enhanced(root, detail)

            # 관련 법령명 추출
            detail['related_law_names'] = self._extract_related_law_names(root, law_name)

            # 원문 저장 (조문이 없는 경우)
            if not detail['articles']:
                detail['raw_content'] = self._extract_full_text(root)
                
            self.logger.info(f"상세 정보 파싱 완료: {law_name} - 조문 {len(detail['articles'])}개, 별표/별첨 {len(detail['attachments'])}개")
                
        except Exception as e:
            self.logger.error(f"상세 정보 파싱 오류: {e}")
            
        return detail
    
    def _parse_admin_rule_detail(self, content: str, law_id: str,
                                law_msn: str, law_name: str) -> Dict[str, Any]:
        """행정규칙 상세 정보 파싱"""
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
            'attachment_pdfs': [],  # PDF 첨부파일 추가
            'raw_content': '',
            'is_admin_rule': True,
            'related_laws': [],
            'related_law_names': []
        }
        
        try:
            # 전처리
            content = self._preprocess_xml_content(content)
            
            # XML 파싱
            root = ET.fromstring(content.encode('utf-8'))
            
            # 행정규칙 기본 정보
            basic_info = root.find('.//행정규칙기본정보')
            if basic_info is not None:
                detail['law_id'] = basic_info.findtext('행정규칙ID', '')
                detail['law_type'] = basic_info.findtext('행정규칙종류', '')
                detail['department'] = basic_info.findtext('소관부처명', '')
                detail['promulgation_date'] = basic_info.findtext('발령일자', '')
                detail['enforcement_date'] = basic_info.findtext('시행일자', '')
            else:
                # 대체 경로
                detail['law_id'] = root.findtext('.//행정규칙ID', '')
                detail['law_type'] = root.findtext('.//행정규칙종류', '')
                detail['department'] = root.findtext('.//소관부처명', '')
                detail['promulgation_date'] = root.findtext('.//발령일자', '')
                detail['enforcement_date'] = root.findtext('.//시행일자', '')
            
            # 조문 추출 (행정규칙도 동일한 구조 사용 가능)
            self._extract_articles(root, detail)
            
            # 부칙 추출
            self._extract_supplementary_provisions(root, detail)
            
            # 별표 추출
            self._extract_attachments(root, detail)

            # PDF 첨부파일 추출 - 개선된 버전
            self._extract_pdf_attachments_enhanced(root, detail)

            # 원문 저장
            if not detail['articles']:
                detail['raw_content'] = self._extract_full_text(root)

            # 관련 법령명 추출
            detail['related_law_names'] = self._extract_related_law_names(root, law_name)

            self.logger.info(f"행정규칙 상세 파싱 완료: {law_name} - 조문 {len(detail['articles'])}개, 별표/별첨 {len(detail['attachments'])}개")
                
        except Exception as e:
            self.logger.error(f"행정규칙 상세 파싱 오류: {e}")

        return detail

    def _extract_related_law_names(self, root: ET.Element, current_name: str) -> List[str]:
        """상세 XML에서 관련 법령명을 수집"""
        related: Set[str] = set()
        candidate_tags = ['관련법령', '관계법령', '연관법령', '법령체계도', '모법령', '하위법령']

        for tag in candidate_tags:
            for elem in root.findall(f'.//{tag}'):
                text = self._collect_text_content(elem)
                related.update(self._extract_law_names_from_text(text))

        for elem in root.iter():
            if '법령명' in elem.tag and elem.text:
                name = self._normalize_candidate_name(elem.text)
                if name:
                    related.add(name)

        current_normalized = self._normalize_law_name(current_name)
        filtered = [name for name in related if name and name != current_normalized]
        filtered.sort()
        return filtered

    def _collect_text_content(self, elem: ET.Element) -> str:
        """요소 내부 텍스트를 공백으로 결합"""
        parts = [text.strip() for text in elem.itertext() if text and text.strip()]
        return ' '.join(parts)

    def _extract_law_names_from_text(self, text: str) -> Set[str]:
        """텍스트 블록에서 법령명 후보 추출"""
        candidates: Set[str] = set()
        if not text:
            return candidates

        segments = re.split(r'[\n\r,;·•▶\-]', text)
        for segment in segments:
            segment = segment.strip()
            if not segment or len(segment) > 80:
                continue

            normalized = self._normalize_candidate_name(segment)
            if not normalized or len(normalized) < 3:
                continue

            if any(keyword in normalized for keyword in self.patterns.LAW_TYPES):
                candidates.add(normalized)

        return candidates

    def _normalize_candidate_name(self, name: str) -> str:
        """관련 법령 후보명을 정규화"""
        if not name:
            return ''

        cleaned = re.sub(r'\s+', ' ', name)
        cleaned = re.sub(r'\(.*?\)', '', cleaned)
        cleaned = re.sub(r'\[시행[^\]]*\]', '', cleaned)
        cleaned = cleaned.strip(' -,:;')
        return self._normalize_law_name(cleaned)

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
    
    def _extract_pdf_attachments_enhanced(self, root: ET.Element, detail: Dict[str, Any]) -> None:
        """PDF 첨부파일 추출 - 개선된 버전 (OCR 지원)"""
        # 별표/별지 정보에서 PDF URL 패턴 추출
        law_name = detail['law_name']
        law_msn = detail['law_msn']
        promulgation_date = detail.get('promulgation_date', '').replace('-', '').replace('.', '')
        enforcement_date = detail.get('enforcement_date', '').replace('-', '').replace('.', '')
        
        # 법령명에서 괄호 제거 (URL에서 문제 일으킬 수 있음)
        clean_law_name = re.sub(r'\([^)]*\)', '', law_name).strip()
        
        # 별표/별지가 있는 경우 PDF 정보 생성
        if detail['attachments']:
            for attachment in detail['attachments']:
                att_type = attachment['type']
                att_num = attachment['number']
                
                if att_type and att_num:
                    # PDF 정보 생성
                    pdf_info = {
                        'file_seq': '',
                        'file_name': f"{clean_law_name}_{att_type}{att_num}",
                        'type': att_type,
                        'content_text': attachment.get('content', ''),  # 텍스트 내용 저장
                        'has_pdf': False,  # PDF 존재 여부
                        'ocr_available': True  # OCR 가능 여부
                    }
                    
                    # 중복 체크
                    if not any(p['file_name'] == pdf_info['file_name'] for p in detail['attachment_pdfs']):
                        detail['attachment_pdfs'].append(pdf_info)
                        self.logger.info(f"별표/별지 발견: {pdf_info['file_name']} (텍스트 {len(pdf_info['content_text'])}자)")
        
        # 로그 출력
        if detail['attachment_pdfs']:
            self.logger.info(f"별표/별지 {len(detail['attachment_pdfs'])}개 발견: {law_name}")
            self.logger.info("💡 PDF 다운로드 대신 텍스트 내용을 사용합니다.")
    
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

    # ===== 자치법규 검색/조회 =====
    def search_ordinance(self, query: str) -> List[Dict[str, Any]]:
        """자치법규 검색 (target=ordin)"""
        params = {
            'OC': self.oc_code,
            'target': 'ordin',
            'type': 'XML',
            'query': query,
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
                self.logger.warning(f"자치법규 검색 실패: {response.status_code}")
                return []

            return self._parse_ordinance_search_response(response.text, query)

        except Exception as e:
            self.logger.error(f"자치법규 검색 오류: {e}")
            return []

    def _parse_ordinance_search_response(self, content: str, search_query: str) -> List[Dict[str, Any]]:
        """자치법규 검색 응답 파싱"""
        results = []

        try:
            content = self._preprocess_xml_content(content)
            root = ET.fromstring(content.encode('utf-8'))

            for item in root.findall('.//law') or root.findall('.//ordin'):
                result = {
                    'law_id': item.findtext('자치법규ID', '') or item.findtext('법령ID', ''),
                    'law_msn': item.findtext('자치법규일련번호', '') or item.findtext('법령일련번호', ''),
                    'law_name': item.findtext('자치법규명', '') or item.findtext('법령명한글', ''),
                    'law_type': '자치법규',
                    'local_gov': item.findtext('자치단체명', '') or item.findtext('지자체명', ''),
                    'promulgation_date': item.findtext('공포일자', ''),
                    'enforcement_date': item.findtext('시행일자', ''),
                    'data_type': 'ordinance',
                    'search_query': search_query
                }

                if result['law_id'] and result['law_name']:
                    results.append(result)

        except ET.ParseError as e:
            self.logger.error(f"자치법규 XML 파싱 오류: {e}")

        return results

    def get_ordinance_detail(self, ordin_id: str, ordin_msn: str, ordin_name: str) -> Optional[Dict[str, Any]]:
        """자치법규 상세 정보 조회"""
        params = {
            'OC': self.oc_code,
            'target': 'ordin',
            'type': 'XML',
            'ID': ordin_id
        }

        try:
            response = self.session.get(
                self.config.LAW_DETAIL_URL,
                params=params,
                timeout=self.config.TIMEOUT
            )

            if response.status_code != 200:
                return None

            return self._parse_ordinance_detail(response.text, ordin_id, ordin_msn, ordin_name)

        except Exception as e:
            self.logger.error(f"자치법규 상세 조회 오류: {e}")
            return None

    def _parse_ordinance_detail(self, content: str, ordin_id: str, ordin_msn: str, ordin_name: str) -> Dict[str, Any]:
        """자치법규 상세 정보 파싱"""
        detail = {
            'law_id': ordin_id,
            'law_msn': ordin_msn,
            'law_name': ordin_name,
            'law_type': '자치법규',
            'local_gov': '',
            'promulgation_date': '',
            'enforcement_date': '',
            'articles': [],
            'supplementary_provisions': [],
            'attachments': [],
            'raw_content': '',
            'data_type': 'ordinance'
        }

        try:
            content = self._preprocess_xml_content(content)
            root = ET.fromstring(content.encode('utf-8'))

            # 기본 정보
            detail['local_gov'] = root.findtext('.//자치단체명', '')
            detail['promulgation_date'] = root.findtext('.//공포일자', '')
            detail['enforcement_date'] = root.findtext('.//시행일자', '')

            # 조문 추출
            self._extract_articles(root, detail)

            # 부칙 추출
            self._extract_supplementary_provisions(root, detail)

            # 별표 추출
            self._extract_attachments(root, detail)

            # 원문 저장
            if not detail['articles']:
                detail['raw_content'] = self._extract_full_text(root)

        except Exception as e:
            self.logger.error(f"자치법규 상세 파싱 오류: {e}")

        return detail

    # ===== 판례 검색/조회 =====
    def search_precedent(self, query: str) -> List[Dict[str, Any]]:
        """판례 검색 (target=prec)"""
        params = {
            'OC': self.oc_code,
            'target': 'prec',
            'type': 'XML',
            'query': query,
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
                self.logger.warning(f"판례 검색 실패: {response.status_code}")
                return []

            return self._parse_precedent_search_response(response.text, query)

        except Exception as e:
            self.logger.error(f"판례 검색 오류: {e}")
            return []

    def _parse_precedent_search_response(self, content: str, search_query: str) -> List[Dict[str, Any]]:
        """판례 검색 응답 파싱"""
        results = []

        try:
            content = self._preprocess_xml_content(content)
            root = ET.fromstring(content.encode('utf-8'))

            for item in root.findall('.//prec'):
                result = {
                    'law_id': item.findtext('판례일련번호', ''),
                    'law_msn': item.findtext('판례일련번호', ''),
                    'law_name': item.findtext('사건명', ''),
                    'law_type': '판례',
                    'case_no': item.findtext('사건번호', ''),
                    'court': item.findtext('법원명', ''),
                    'decision_date': item.findtext('선고일자', ''),
                    'decision_type': item.findtext('사건종류명', ''),
                    'data_type': 'precedent',
                    'search_query': search_query
                }

                if result['law_id'] and result['law_name']:
                    results.append(result)

        except ET.ParseError as e:
            self.logger.error(f"판례 XML 파싱 오류: {e}")

        return results

    def get_precedent_detail(self, prec_id: str, prec_name: str) -> Optional[Dict[str, Any]]:
        """판례 상세 정보 조회"""
        params = {
            'OC': self.oc_code,
            'target': 'prec',
            'type': 'XML',
            'ID': prec_id
        }

        try:
            response = self.session.get(
                self.config.LAW_DETAIL_URL,
                params=params,
                timeout=self.config.TIMEOUT
            )

            if response.status_code != 200:
                return None

            return self._parse_precedent_detail(response.text, prec_id, prec_name)

        except Exception as e:
            self.logger.error(f"판례 상세 조회 오류: {e}")
            return None

    def _parse_precedent_detail(self, content: str, prec_id: str, prec_name: str) -> Dict[str, Any]:
        """판례 상세 정보 파싱"""
        detail = {
            'law_id': prec_id,
            'law_msn': prec_id,
            'law_name': prec_name,
            'law_type': '판례',
            'case_no': '',
            'court': '',
            'decision_date': '',
            'decision_type': '',
            'judgment_summary': '',
            'judgment_content': '',
            'reference_articles': '',
            'reference_cases': '',
            'raw_content': '',
            'data_type': 'precedent'
        }

        try:
            content = self._preprocess_xml_content(content)
            root = ET.fromstring(content.encode('utf-8'))

            detail['case_no'] = root.findtext('.//사건번호', '')
            detail['court'] = root.findtext('.//법원명', '')
            detail['decision_date'] = root.findtext('.//선고일자', '')
            detail['decision_type'] = root.findtext('.//사건종류명', '')
            detail['judgment_summary'] = root.findtext('.//판시사항', '')
            detail['judgment_content'] = root.findtext('.//판결요지', '') or root.findtext('.//전문', '')
            detail['reference_articles'] = root.findtext('.//참조조문', '')
            detail['reference_cases'] = root.findtext('.//참조판례', '')
            detail['raw_content'] = root.findtext('.//전문', '')

        except Exception as e:
            self.logger.error(f"판례 상세 파싱 오류: {e}")

        return detail

    # ===== 헌재결정례 검색/조회 =====
    def search_constitutional_decision(self, query: str) -> List[Dict[str, Any]]:
        """헌재결정례 검색 (target=detc)"""
        params = {
            'OC': self.oc_code,
            'target': 'detc',
            'type': 'XML',
            'query': query,
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
                self.logger.warning(f"헌재결정례 검색 실패: {response.status_code}")
                return []

            return self._parse_constitutional_search_response(response.text, query)

        except Exception as e:
            self.logger.error(f"헌재결정례 검색 오류: {e}")
            return []

    def _parse_constitutional_search_response(self, content: str, search_query: str) -> List[Dict[str, Any]]:
        """헌재결정례 검색 응답 파싱"""
        results = []

        try:
            content = self._preprocess_xml_content(content)
            root = ET.fromstring(content.encode('utf-8'))

            for item in root.findall('.//detc'):
                result = {
                    'law_id': item.findtext('헌재결정례일련번호', ''),
                    'law_msn': item.findtext('헌재결정례일련번호', ''),
                    'law_name': item.findtext('사건명', ''),
                    'law_type': '헌재결정례',
                    'case_no': item.findtext('사건번호', ''),
                    'decision_date': item.findtext('종국일자', ''),
                    'data_type': 'constitutional',
                    'search_query': search_query
                }

                if result['law_id'] and result['law_name']:
                    results.append(result)

        except ET.ParseError as e:
            self.logger.error(f"헌재결정례 XML 파싱 오류: {e}")

        return results

    def get_constitutional_detail(self, detc_id: str, detc_name: str) -> Optional[Dict[str, Any]]:
        """헌재결정례 상세 정보 조회"""
        params = {
            'OC': self.oc_code,
            'target': 'detc',
            'type': 'XML',
            'ID': detc_id
        }

        try:
            response = self.session.get(
                self.config.LAW_DETAIL_URL,
                params=params,
                timeout=self.config.TIMEOUT
            )

            if response.status_code != 200:
                return None

            return self._parse_constitutional_detail(response.text, detc_id, detc_name)

        except Exception as e:
            self.logger.error(f"헌재결정례 상세 조회 오류: {e}")
            return None

    def _parse_constitutional_detail(self, content: str, detc_id: str, detc_name: str) -> Dict[str, Any]:
        """헌재결정례 상세 정보 파싱"""
        detail = {
            'law_id': detc_id,
            'law_msn': detc_id,
            'law_name': detc_name,
            'law_type': '헌재결정례',
            'case_no': '',
            'decision_date': '',
            'case_type': '',
            'judgment_summary': '',
            'decision_summary': '',
            'full_text': '',
            'reference_articles': '',
            'reference_cases': '',
            'data_type': 'constitutional'
        }

        try:
            content = self._preprocess_xml_content(content)
            root = ET.fromstring(content.encode('utf-8'))

            detail['case_no'] = root.findtext('.//사건번호', '')
            detail['decision_date'] = root.findtext('.//종국일자', '')
            detail['case_type'] = root.findtext('.//사건종류명', '')
            detail['judgment_summary'] = root.findtext('.//판시사항', '')
            detail['decision_summary'] = root.findtext('.//결정요지', '')
            detail['full_text'] = root.findtext('.//전문', '')
            detail['reference_articles'] = root.findtext('.//참조조문', '')
            detail['reference_cases'] = root.findtext('.//참조판례', '')

        except Exception as e:
            self.logger.error(f"헌재결정례 상세 파싱 오류: {e}")

        return detail

    # ===== 법령해석례 검색/조회 =====
    def search_interpretation(self, query: str) -> List[Dict[str, Any]]:
        """법령해석례 검색 (target=expc)"""
        params = {
            'OC': self.oc_code,
            'target': 'expc',
            'type': 'XML',
            'query': query,
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
                self.logger.warning(f"법령해석례 검색 실패: {response.status_code}")
                return []

            return self._parse_interpretation_search_response(response.text, query)

        except Exception as e:
            self.logger.error(f"법령해석례 검색 오류: {e}")
            return []

    def _parse_interpretation_search_response(self, content: str, search_query: str) -> List[Dict[str, Any]]:
        """법령해석례 검색 응답 파싱"""
        results = []

        try:
            content = self._preprocess_xml_content(content)
            root = ET.fromstring(content.encode('utf-8'))

            for item in root.findall('.//expc'):
                result = {
                    'law_id': item.findtext('법령해석례일련번호', ''),
                    'law_msn': item.findtext('법령해석례일련번호', ''),
                    'law_name': item.findtext('안건명', ''),
                    'law_type': '법령해석례',
                    'case_no': item.findtext('안건번호', ''),
                    'inquiry_org': item.findtext('질의기관명', ''),
                    'reply_org': item.findtext('회신기관명', ''),
                    'reply_date': item.findtext('회신일자', ''),
                    'data_type': 'interpretation',
                    'search_query': search_query
                }

                if result['law_id'] and result['law_name']:
                    results.append(result)

        except ET.ParseError as e:
            self.logger.error(f"법령해석례 XML 파싱 오류: {e}")

        return results

    def get_interpretation_detail(self, expc_id: str, expc_name: str) -> Optional[Dict[str, Any]]:
        """법령해석례 상세 정보 조회"""
        params = {
            'OC': self.oc_code,
            'target': 'expc',
            'type': 'XML',
            'ID': expc_id
        }

        try:
            response = self.session.get(
                self.config.LAW_DETAIL_URL,
                params=params,
                timeout=self.config.TIMEOUT
            )

            if response.status_code != 200:
                return None

            return self._parse_interpretation_detail(response.text, expc_id, expc_name)

        except Exception as e:
            self.logger.error(f"법령해석례 상세 조회 오류: {e}")
            return None

    def _parse_interpretation_detail(self, content: str, expc_id: str, expc_name: str) -> Dict[str, Any]:
        """법령해석례 상세 정보 파싱"""
        detail = {
            'law_id': expc_id,
            'law_msn': expc_id,
            'law_name': expc_name,
            'law_type': '법령해석례',
            'case_no': '',
            'interpretation_date': '',
            'interpretation_org': '',
            'inquiry_org': '',
            'inquiry_summary': '',
            'reply': '',
            'reason': '',
            'data_type': 'interpretation'
        }

        try:
            content = self._preprocess_xml_content(content)
            root = ET.fromstring(content.encode('utf-8'))

            detail['case_no'] = root.findtext('.//안건번호', '')
            detail['interpretation_date'] = root.findtext('.//해석일자', '')
            detail['interpretation_org'] = root.findtext('.//해석기관명', '')
            detail['inquiry_org'] = root.findtext('.//질의기관명', '')
            detail['inquiry_summary'] = root.findtext('.//질의요지', '')
            detail['reply'] = root.findtext('.//회답', '')
            detail['reason'] = root.findtext('.//이유', '')

        except Exception as e:
            self.logger.error(f"법령해석례 상세 파싱 오류: {e}")

        return detail

    # ===== 행정심판례 검색/조회 =====
    def search_admin_decision(self, query: str) -> List[Dict[str, Any]]:
        """행정심판례 검색 (target=decc)"""
        params = {
            'OC': self.oc_code,
            'target': 'decc',
            'type': 'XML',
            'query': query,
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
                self.logger.warning(f"행정심판례 검색 실패: {response.status_code}")
                return []

            return self._parse_admin_decision_search_response(response.text, query)

        except Exception as e:
            self.logger.error(f"행정심판례 검색 오류: {e}")
            return []

    def _parse_admin_decision_search_response(self, content: str, search_query: str) -> List[Dict[str, Any]]:
        """행정심판례 검색 응답 파싱"""
        results = []

        try:
            content = self._preprocess_xml_content(content)
            root = ET.fromstring(content.encode('utf-8'))

            for item in root.findall('.//decc'):
                result = {
                    'law_id': item.findtext('행정심판재결례일련번호', ''),
                    'law_msn': item.findtext('행정심판재결례일련번호', ''),
                    'law_name': item.findtext('사건명', ''),
                    'law_type': '행정심판례',
                    'case_no': item.findtext('사건번호', ''),
                    'disposal_date': item.findtext('처분일자', ''),
                    'decision_date': item.findtext('의결일자', ''),
                    'disposal_org': item.findtext('처분청', ''),
                    'decision_org': item.findtext('재결청', ''),
                    'decision_type': item.findtext('재결구분명', ''),
                    'data_type': 'admin_decision',
                    'search_query': search_query
                }

                if result['law_id'] and result['law_name']:
                    results.append(result)

        except ET.ParseError as e:
            self.logger.error(f"행정심판례 XML 파싱 오류: {e}")

        return results

    def get_admin_decision_detail(self, decc_id: str, decc_name: str) -> Optional[Dict[str, Any]]:
        """행정심판례 상세 정보 조회"""
        params = {
            'OC': self.oc_code,
            'target': 'decc',
            'type': 'XML',
            'ID': decc_id
        }

        try:
            response = self.session.get(
                self.config.LAW_DETAIL_URL,
                params=params,
                timeout=self.config.TIMEOUT
            )

            if response.status_code != 200:
                return None

            return self._parse_admin_decision_detail(response.text, decc_id, decc_name)

        except Exception as e:
            self.logger.error(f"행정심판례 상세 조회 오류: {e}")
            return None

    def _parse_admin_decision_detail(self, content: str, decc_id: str, decc_name: str) -> Dict[str, Any]:
        """행정심판례 상세 정보 파싱"""
        detail = {
            'law_id': decc_id,
            'law_msn': decc_id,
            'law_name': decc_name,
            'law_type': '행정심판례',
            'case_no': '',
            'disposal_date': '',
            'decision_date': '',
            'disposal_org': '',
            'decision_org': '',
            'decision_type': '',
            'main_text': '',
            'claim_purport': '',
            'reason': '',
            'decision_summary': '',
            'data_type': 'admin_decision'
        }

        try:
            content = self._preprocess_xml_content(content)
            root = ET.fromstring(content.encode('utf-8'))

            detail['case_no'] = root.findtext('.//사건번호', '')
            detail['disposal_date'] = root.findtext('.//처분일자', '')
            detail['decision_date'] = root.findtext('.//의결일자', '')
            detail['disposal_org'] = root.findtext('.//처분청', '')
            detail['decision_org'] = root.findtext('.//재결청', '')
            detail['decision_type'] = root.findtext('.//재결례유형명', '')
            detail['main_text'] = root.findtext('.//주문', '')
            detail['claim_purport'] = root.findtext('.//청구취지', '')
            detail['reason'] = root.findtext('.//이유', '')
            detail['decision_summary'] = root.findtext('.//재결요지', '')

        except Exception as e:
            self.logger.error(f"행정심판례 상세 파싱 오류: {e}")

        return detail

    # ===== 조약 검색/조회 =====
    def search_treaty(self, query: str) -> List[Dict[str, Any]]:
        """조약 검색 (target=trty)"""
        params = {
            'OC': self.oc_code,
            'target': 'trty',
            'type': 'XML',
            'query': query,
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
                self.logger.warning(f"조약 검색 실패: {response.status_code}")
                return []

            return self._parse_treaty_search_response(response.text, query)

        except Exception as e:
            self.logger.error(f"조약 검색 오류: {e}")
            return []

    def _parse_treaty_search_response(self, content: str, search_query: str) -> List[Dict[str, Any]]:
        """조약 검색 응답 파싱"""
        results = []

        try:
            content = self._preprocess_xml_content(content)
            root = ET.fromstring(content.encode('utf-8'))

            for item in root.findall('.//trty'):
                result = {
                    'law_id': item.findtext('조약일련번호', ''),
                    'law_msn': item.findtext('조약일련번호', ''),
                    'law_name': item.findtext('조약명', ''),
                    'law_type': '조약',
                    'treaty_no': item.findtext('조약번호', ''),
                    'signing_date': item.findtext('서명일자', ''),
                    'enforcement_date': item.findtext('발효일자', ''),
                    'country': item.findtext('체결국가', ''),
                    'data_type': 'treaty',
                    'search_query': search_query
                }

                if result['law_id'] and result['law_name']:
                    results.append(result)

        except ET.ParseError as e:
            self.logger.error(f"조약 XML 파싱 오류: {e}")

        return results

    def get_treaty_detail(self, treaty_id: str, treaty_name: str) -> Optional[Dict[str, Any]]:
        """조약 상세 정보 조회"""
        params = {
            'OC': self.oc_code,
            'target': 'trty',
            'type': 'XML',
            'ID': treaty_id
        }

        try:
            response = self.session.get(
                self.config.LAW_DETAIL_URL,
                params=params,
                timeout=self.config.TIMEOUT
            )

            if response.status_code != 200:
                return None

            return self._parse_treaty_detail(response.text, treaty_id, treaty_name)

        except Exception as e:
            self.logger.error(f"조약 상세 조회 오류: {e}")
            return None

    def _parse_treaty_detail(self, content: str, treaty_id: str, treaty_name: str) -> Dict[str, Any]:
        """조약 상세 정보 파싱"""
        detail = {
            'law_id': treaty_id,
            'law_msn': treaty_id,
            'law_name': treaty_name,
            'law_type': '조약',
            'treaty_no': '',
            'signing_date': '',
            'enforcement_date': '',
            'country': '',
            'treaty_type': '',
            'full_text': '',
            'articles': [],
            'data_type': 'treaty'
        }

        try:
            content = self._preprocess_xml_content(content)
            root = ET.fromstring(content.encode('utf-8'))

            detail['treaty_no'] = root.findtext('.//조약번호', '')
            detail['signing_date'] = root.findtext('.//서명일자', '')
            detail['enforcement_date'] = root.findtext('.//발효일자', '')
            detail['country'] = root.findtext('.//체결국가', '')
            detail['treaty_type'] = root.findtext('.//조약유형', '')
            detail['full_text'] = root.findtext('.//조약본문', '') or self._extract_full_text(root)

            # 조문 추출
            self._extract_articles(root, detail)

        except Exception as e:
            self.logger.error(f"조약 상세 파싱 오류: {e}")

        return detail

    # ===== 법령 체계도 검색 메서드 =====
    def search_law_hierarchy_list(self, query: str) -> List[Dict[str, Any]]:
        """법령 체계도 목록 검색 (target=lsStmd)"""
        params = {
            'OC': self.oc_code,
            'target': 'lsStmd',
            'type': 'XML',
            'query': query,
            'display': self.config.RESULTS_PER_PAGE
        }

        try:
            time.sleep(self.config.DEFAULT_DELAY)
            response = self.session.get(
                self.config.LAW_SEARCH_URL,
                params=params,
                timeout=self.config.TIMEOUT
            )

            if response.status_code != 200:
                self.logger.error(f"체계도 목록 검색 실패: HTTP {response.status_code}")
                return []

            return self._parse_hierarchy_list_response(response.text, query)

        except Exception as e:
            self.logger.error(f"체계도 목록 검색 오류: {e}")
            return []

    def _parse_hierarchy_list_response(self, content: str, query: str) -> List[Dict[str, Any]]:
        """법령 체계도 목록 응답 파싱"""
        results = []

        try:
            content = self._preprocess_xml_content(content)
            root = ET.fromstring(content.encode('utf-8'))

            for item in root.findall('.//law') or root.findall('.//lsStmd'):
                law_data = {
                    'law_id': item.findtext('법령ID', '') or item.findtext('.//법령ID', ''),
                    'law_msn': item.findtext('법령일련번호', '') or item.findtext('.//법령일련번호', ''),
                    'law_name': item.findtext('법령명', '') or item.findtext('.//법령명', ''),
                    'law_type': item.findtext('법령구분명', '') or item.findtext('.//법령구분명', ''),
                    'promulgation_date': item.findtext('공포일자', '') or item.findtext('.//공포일자', ''),
                    'enforcement_date': item.findtext('시행일자', '') or item.findtext('.//시행일자', ''),
                    'department': item.findtext('소관부처명', '') or item.findtext('.//소관부처명', ''),
                    'search_query': query,
                    'data_type': 'hierarchy'
                }

                if law_data['law_id'] or law_data['law_msn']:
                    results.append(law_data)

        except ET.ParseError as e:
            self.logger.error(f"체계도 목록 XML 파싱 오류: {e}")

        return results

    def get_law_hierarchy_detail(self, law_id: str = '', law_msn: str = '') -> Optional[Dict[str, Any]]:
        """법령 체계도 본문 조회 (target=lsStmd) - 상하위법 정보 포함"""
        params = {
            'OC': self.oc_code,
            'target': 'lsStmd',
            'type': 'XML'
        }

        if law_id:
            params['ID'] = law_id
        elif law_msn:
            params['MST'] = law_msn
        else:
            self.logger.error("법령 ID 또는 MST가 필요합니다")
            return None

        try:
            time.sleep(self.config.DEFAULT_DELAY)
            response = self.session.get(
                self.config.LAW_DETAIL_URL,
                params=params,
                timeout=self.config.TIMEOUT
            )

            if response.status_code != 200:
                self.logger.error(f"체계도 본문 조회 실패: HTTP {response.status_code}")
                return None

            return self._parse_hierarchy_detail_response(response.text)

        except Exception as e:
            self.logger.error(f"체계도 본문 조회 오류: {e}")
            return None

    def _parse_hierarchy_detail_response(self, content: str) -> Optional[Dict[str, Any]]:
        """법령 체계도 본문 응답 파싱 - 상하위법 구조 추출"""
        try:
            content = self._preprocess_xml_content(content)
            root = ET.fromstring(content.encode('utf-8'))

            hierarchy = {
                'law_id': '',
                'law_msn': '',
                'law_name': '',
                'law_type': '',
                'enforcement_date': '',
                'promulgation_date': '',
                'related_laws': {
                    'laws': [],          # 법률
                    'enforcement_decrees': [],  # 시행령
                    'enforcement_rules': [],    # 시행규칙
                    'admin_rules': []    # 행정규칙 (고시, 훈령 등)
                },
                'all_related_names': []  # 모든 관련 법령명 리스트
            }

            # 기본 정보 추출
            basic_info = root.find('.//기본정보') or root
            hierarchy['law_id'] = basic_info.findtext('.//법령ID', '') or root.findtext('.//법령ID', '')
            hierarchy['law_msn'] = basic_info.findtext('.//법령일련번호', '') or root.findtext('.//법령일련번호', '')
            hierarchy['law_name'] = basic_info.findtext('.//법령명', '') or root.findtext('.//법령명', '')
            hierarchy['law_type'] = basic_info.findtext('.//법종구분', '') or root.findtext('.//법종구분', '')
            hierarchy['enforcement_date'] = basic_info.findtext('.//시행일자', '') or root.findtext('.//시행일자', '')
            hierarchy['promulgation_date'] = basic_info.findtext('.//공포일자', '') or root.findtext('.//공포일자', '')

            # 상하위법 정보 추출
            hierarchy_section = root.find('.//상하위법') or root

            # 법률 추출
            for law_elem in hierarchy_section.findall('.//법률') or []:
                law_info = self._extract_hierarchy_law_info(law_elem, '법률')
                if law_info:
                    hierarchy['related_laws']['laws'].append(law_info)
                    hierarchy['all_related_names'].append(law_info['name'])

            # 시행령 추출
            for decree_elem in hierarchy_section.findall('.//시행령') or []:
                decree_info = self._extract_hierarchy_law_info(decree_elem, '시행령')
                if decree_info:
                    hierarchy['related_laws']['enforcement_decrees'].append(decree_info)
                    hierarchy['all_related_names'].append(decree_info['name'])

            # 시행규칙 추출
            for rule_elem in hierarchy_section.findall('.//시행규칙') or []:
                rule_info = self._extract_hierarchy_law_info(rule_elem, '시행규칙')
                if rule_info:
                    hierarchy['related_laws']['enforcement_rules'].append(rule_info)
                    hierarchy['all_related_names'].append(rule_info['name'])

            # 행정규칙 추출 (고시, 훈령 등)
            for admin_type in ['행정규칙', '고시', '훈령', '예규', '기타']:
                for admin_elem in hierarchy_section.findall(f'.//{admin_type}') or []:
                    admin_info = self._extract_hierarchy_law_info(admin_elem, admin_type)
                    if admin_info:
                        hierarchy['related_laws']['admin_rules'].append(admin_info)
                        hierarchy['all_related_names'].append(admin_info['name'])

            # 본문에서 추가 법령 정보 추출 (텍스트 파싱)
            self._extract_additional_hierarchy_laws(root, hierarchy)

            return hierarchy

        except ET.ParseError as e:
            self.logger.error(f"체계도 본문 XML 파싱 오류: {e}")
            return None
        except Exception as e:
            self.logger.error(f"체계도 본문 처리 오류: {e}")
            return None

    def _extract_hierarchy_law_info(self, elem: ET.Element, law_type: str) -> Optional[Dict[str, str]]:
        """체계도에서 개별 법령 정보 추출"""
        # 텍스트로 법령명 추출 시도
        law_name = elem.text.strip() if elem.text else ''

        # 하위 요소에서 법령명 추출 시도
        if not law_name:
            law_name = elem.findtext('.//법령명', '') or elem.findtext('.//명칭', '')

        # 속성에서 법령명 추출 시도
        if not law_name:
            law_name = elem.get('법령명', '') or elem.get('명칭', '')

        if not law_name:
            return None

        return {
            'name': law_name.strip(),
            'type': law_type,
            'id': elem.findtext('.//법령ID', '') or elem.get('법령ID', ''),
            'msn': elem.findtext('.//법령일련번호', '') or elem.get('법령일련번호', ''),
            'enforcement_date': elem.findtext('.//시행일자', '') or elem.get('시행일자', ''),
            'promulgation_date': elem.findtext('.//공포일자', '') or elem.get('공포일자', '')
        }

    def _extract_additional_hierarchy_laws(self, root: ET.Element, hierarchy: Dict[str, Any]):
        """XML 전체에서 추가 법령 정보 추출"""
        # 본문에서 행정규칙 정보 추출 (다양한 구조 지원)
        admin_patterns = ['행정규칙', '하위행정규칙', '관련행정규칙', '위임행정규칙']

        for pattern in admin_patterns:
            section = root.find(f'.//{pattern}')
            if section is not None:
                # 섹션 내의 모든 항목 검색
                for child in section:
                    if child.text and child.text.strip():
                        name = child.text.strip()
                        if name not in hierarchy['all_related_names']:
                            tag_name = child.tag if child.tag else '행정규칙'
                            hierarchy['related_laws']['admin_rules'].append({
                                'name': name,
                                'type': tag_name,
                                'id': child.get('법령ID', '') or child.findtext('.//법령ID', ''),
                                'msn': child.get('행정규칙일련번호', '') or child.findtext('.//행정규칙일련번호', ''),
                                'enforcement_date': '',
                                'promulgation_date': ''
                            })
                            hierarchy['all_related_names'].append(name)

    def _extract_law_keywords(self, law_name: str) -> List[str]:
        """법령명에서 검색 키워드 추출 (행정규칙 검색용)"""
        keywords = []

        # 법, 시행령, 시행규칙 등 접미사 제거
        suffixes = ['법', '시행령', '시행규칙', '규정', '규칙', '지침', '고시', '훈령', '예규']
        base_name = law_name
        for suffix in suffixes:
            if base_name.endswith(suffix):
                base_name = base_name[:-len(suffix)]
                break

        if base_name:
            keywords.append(base_name)

        # 띄어쓰기 없는 버전과 있는 버전 모두 시도
        if ' ' in base_name:
            keywords.append(base_name.replace(' ', ''))

        return keywords

    def _search_related_admin_rules(self, law_name: str, seen_ids: set) -> List[Dict[str, Any]]:
        """법령명 키워드로 관련 행정규칙 검색"""
        results = []
        keywords = self._extract_law_keywords(law_name)

        self.logger.info(f"행정규칙 키워드 검색: {keywords}")

        for keyword in keywords:
            if len(keyword) < 2:
                continue

            try:
                params = {
                    'OC': self.oc_code,
                    'target': 'admrul',
                    'type': 'XML',
                    'query': keyword,
                    'display': '100',
                    'page': '1'
                }

                response = self.session.get(
                    self.config.ADMIN_RULE_SEARCH_URL,
                    params=params,
                    timeout=self.config.TIMEOUT
                )

                if response.status_code == 200:
                    rules = self._parse_admin_rule_search_response(response.text, keyword)

                    for rule in rules:
                        rule_id = rule.get('law_id', '')
                        if rule_id and rule_id not in seen_ids:
                            # 키워드가 실제로 규칙명에 포함되어 있는지 확인
                            rule_name = rule.get('law_name', '')
                            if keyword in rule_name:
                                seen_ids.add(rule_id)
                                rule['hierarchy_source'] = f'관련 행정규칙 ({keyword})'
                                results.append(rule)
                                self.logger.info(f"관련 행정규칙 발견: {rule_name}")

            except Exception as e:
                self.logger.error(f"행정규칙 키워드 검색 오류 ({keyword}): {e}")

        return results

    def _get_delegated_admin_rules(self, law_id: str) -> List[str]:
        """위임법령 조회 API를 통해 위임된 행정규칙 목록 조회"""
        delegated_rules = []

        if not law_id:
            return delegated_rules

        try:
            params = {
                'OC': self.oc_code,
                'target': 'lsDelegated',
                'type': 'XML',
                'ID': law_id
            }

            self.logger.info(f"위임법령 조회: ID={law_id}")

            response = self.session.get(
                'http://www.law.go.kr/DRF/lawService.do',
                params=params,
                timeout=self.config.TIMEOUT
            )

            if response.status_code == 200:
                content = self._preprocess_xml_content(response.text)
                root = ET.fromstring(content.encode('utf-8'))

                # 위임행정규칙제목 추출
                for elem in root.findall('.//위임행정규칙제목'):
                    if elem.text and elem.text.strip():
                        rule_name = elem.text.strip()
                        if rule_name not in delegated_rules:
                            delegated_rules.append(rule_name)
                            self.logger.info(f"위임 행정규칙 발견: {rule_name}")

                # 위임법령제목도 추출 (시행령, 시행규칙 등)
                for elem in root.findall('.//위임법령제목'):
                    if elem.text and elem.text.strip():
                        rule_name = elem.text.strip()
                        if rule_name not in delegated_rules:
                            delegated_rules.append(rule_name)
                            self.logger.info(f"위임 법령 발견: {rule_name}")

        except ET.ParseError as e:
            self.logger.error(f"위임법령 XML 파싱 오류: {e}")
        except Exception as e:
            self.logger.error(f"위임법령 조회 오류: {e}")

        return delegated_rules

    def _search_delegated_rules(self, law_id: str, seen_ids: set) -> List[Dict[str, Any]]:
        """위임된 행정규칙을 검색하여 상세 정보 수집"""
        results = []

        # 위임된 법령/행정규칙 목록 조회
        delegated_names = self._get_delegated_admin_rules(law_id)

        self.logger.info(f"위임 법령/행정규칙 {len(delegated_names)}개 발견")

        for rule_name in delegated_names:
            # 먼저 행정규칙으로 검색
            search_results = self._search_admin_rule(rule_name)

            # 결과가 없으면 일반 법령으로 검색
            if not search_results:
                search_results = self._search_exact_match(rule_name)

            if not search_results:
                search_results = self._search_general_law(rule_name)

            for rule in search_results:
                rule_id = rule.get('law_id', '')
                if rule_id and rule_id not in seen_ids:
                    seen_ids.add(rule_id)
                    rule['hierarchy_source'] = f'위임 법령 ({rule_name})'
                    results.append(rule)
                    self.logger.info(f"위임 법령 추가: {rule.get('law_name', '')}")

        return results

    def search_with_hierarchy(self, query: str, progress_callback=None) -> Dict[str, Any]:
        """법령 체계도 기반 통합 검색 - 상위법과 모든 하위법령을 함께 검색"""
        result = {
            'query': query,
            'hierarchy_info': None,
            'laws': [],
            'search_summary': {
                'total': 0,
                'laws_count': 0,
                'decrees_count': 0,
                'rules_count': 0,
                'admin_rules_count': 0
            }
        }

        # Step 1: 체계도 목록 검색
        if progress_callback:
            progress_callback(0.1, "체계도 목록 검색 중...")

        hierarchy_list = self.search_law_hierarchy_list(query)

        if not hierarchy_list:
            self.logger.warning(f"체계도 검색 결과 없음: {query}")
            # 일반 법령 검색으로 폴백
            return self._fallback_to_regular_search(query, progress_callback)

        # Step 2: 체계도 본문 조회 (가장 유사한 결과 사용)
        if progress_callback:
            progress_callback(0.2, "체계도 상세 정보 조회 중...")

        target_law = self._find_best_match(hierarchy_list, query)
        hierarchy_detail = self.get_law_hierarchy_detail(
            law_id=target_law.get('law_id', ''),
            law_msn=target_law.get('law_msn', '')
        )

        if not hierarchy_detail:
            self.logger.warning("체계도 상세 정보 조회 실패")
            return self._fallback_to_regular_search(query, progress_callback)

        result['hierarchy_info'] = hierarchy_detail

        # Step 3: 체계도에서 추출한 모든 법령 검색
        all_law_names = hierarchy_detail.get('all_related_names', [])

        # 기본 법령명도 추가
        if hierarchy_detail.get('law_name'):
            if hierarchy_detail['law_name'] not in all_law_names:
                all_law_names.insert(0, hierarchy_detail['law_name'])

        if not all_law_names:
            all_law_names = [query]

        self.logger.info(f"체계도에서 {len(all_law_names)}개 법령 발견: {all_law_names}")

        # Step 4: 각 법령 검색 및 상세 정보 수집
        if progress_callback:
            progress_callback(0.3, f"{len(all_law_names)}개 법령 검색 중...")

        collected_laws = []
        seen_ids = set()

        for idx, law_name in enumerate(all_law_names):
            if progress_callback:
                progress = 0.3 + (0.6 * (idx + 1) / len(all_law_names))
                progress_callback(progress, f"검색 중: {law_name}")

            # 법령 검색 (정확한 매칭 사용)
            search_results = self._search_exact_match(law_name)

            # 결과가 없으면 일반 검색 시도
            if not search_results:
                search_results = self._search_general_law(law_name)

            # 행정규칙 검색도 시도
            if not search_results:
                search_results = self._search_admin_rule(law_name)

            for law in search_results:
                if law['law_id'] not in seen_ids:
                    seen_ids.add(law['law_id'])
                    law['hierarchy_source'] = law_name
                    collected_laws.append(law)

        # Step 5: 위임법령 조회 API를 통한 위임 행정규칙 검색
        # 법률, 시행령, 시행규칙 모두에 대해 위임법령 조회 수행
        if progress_callback:
            progress_callback(0.75, "위임 법령/행정규칙 검색 중...")

        # 수집된 모든 법령의 ID 목록 (법률, 시행령, 시행규칙)
        law_ids_to_check = []

        # 기본 법령 ID 추가
        main_law_id = hierarchy_detail.get('law_id', '') or target_law.get('law_id', '')
        if main_law_id:
            law_ids_to_check.append(('기본 법령', main_law_id))

        # 수집된 법령들의 ID 추가 (시행령, 시행규칙 포함)
        for law in collected_laws:
            law_id = law.get('law_id', '')
            law_name = law.get('law_name', '')
            if law_id and law_id not in [lid for _, lid in law_ids_to_check]:
                # 행정규칙이 아닌 법령만 추가 (법률, 시행령, 시행규칙)
                if not law.get('is_admin_rule', False):
                    law_ids_to_check.append((law_name, law_id))

        self.logger.info(f"위임법령 조회 대상: {len(law_ids_to_check)}개 법령")

        # 각 법령에 대해 위임법령 조회
        total_delegated = 0
        for idx, (source_name, law_id) in enumerate(law_ids_to_check):
            if progress_callback:
                progress = 0.75 + (0.15 * (idx + 1) / max(len(law_ids_to_check), 1))
                progress_callback(progress, f"위임법령 조회 중: {source_name[:20]}...")

            delegated_rules = self._search_delegated_rules(law_id, seen_ids)
            if delegated_rules:
                self.logger.info(f"{source_name}의 위임 법령/행정규칙 {len(delegated_rules)}개 추가")
                # 출처 정보 업데이트
                for rule in delegated_rules:
                    rule['hierarchy_source'] = f"위임 ({source_name})"
                collected_laws.extend(delegated_rules)
                total_delegated += len(delegated_rules)

        self.logger.info(f"총 위임 법령/행정규칙 {total_delegated}개 추가")

        # Step 6: 관련 행정규칙 키워드 검색 (법령명에서 키워드 추출하여 검색)
        if progress_callback:
            progress_callback(0.92, "관련 행정규칙 검색 중...")

        # 기본 법령명에서 키워드 추출하여 행정규칙 검색
        main_law_name = hierarchy_detail.get('law_name', query)
        related_admin_rules = self._search_related_admin_rules(main_law_name, seen_ids)

        if related_admin_rules:
            self.logger.info(f"관련 행정규칙 {len(related_admin_rules)}개 추가")
            collected_laws.extend(related_admin_rules)

        result['laws'] = collected_laws

        # 통계 업데이트
        result['search_summary']['total'] = len(collected_laws)
        for law in collected_laws:
            law_type = law.get('law_type', '')
            if law.get('is_admin_rule'):
                result['search_summary']['admin_rules_count'] += 1
            elif '시행령' in law_type or '시행령' in law.get('law_name', ''):
                result['search_summary']['decrees_count'] += 1
            elif '시행규칙' in law_type or '시행규칙' in law.get('law_name', ''):
                result['search_summary']['rules_count'] += 1
            else:
                result['search_summary']['laws_count'] += 1

        if progress_callback:
            progress_callback(1.0, "검색 완료")

        return result

    def _find_best_match(self, results: List[Dict[str, Any]], query: str) -> Dict[str, Any]:
        """검색 결과에서 가장 유사한 항목 찾기"""
        if not results:
            return {}

        best_match = results[0]
        best_similarity = 0

        for result in results:
            law_name = result.get('law_name', '')
            similarity = self._calculate_similarity(query, law_name)

            if similarity > best_similarity:
                best_similarity = similarity
                best_match = result

        return best_match

    def _fallback_to_regular_search(self, query: str, progress_callback=None) -> Dict[str, Any]:
        """체계도 검색 실패 시 일반 검색으로 폴백"""
        if progress_callback:
            progress_callback(0.5, "일반 검색으로 전환...")

        result = {
            'query': query,
            'hierarchy_info': None,
            'laws': [],
            'search_summary': {
                'total': 0,
                'laws_count': 0,
                'decrees_count': 0,
                'rules_count': 0,
                'admin_rules_count': 0
            },
            'fallback': True
        }

        # 일반 법령 검색
        laws = self._search_with_variations(query)
        result['laws'] = laws
        result['search_summary']['total'] = len(laws)

        for law in laws:
            if law.get('is_admin_rule'):
                result['search_summary']['admin_rules_count'] += 1
            else:
                result['search_summary']['laws_count'] += 1

        if progress_callback:
            progress_callback(1.0, "검색 완료")

        return result

    # ===== 통합 검색 메서드 =====
    def search_by_type(self, query: str, data_type: str) -> List[Dict[str, Any]]:
        """데이터 유형별 검색"""
        search_methods = {
            'law': lambda q: self.search_single_law(q),
            'ordinance': self.search_ordinance,
            'precedent': self.search_precedent,
            'constitutional': self.search_constitutional_decision,
            'interpretation': self.search_interpretation,
            'admin_decision': self.search_admin_decision,
            'treaty': self.search_treaty
        }

        method = search_methods.get(data_type)
        if method:
            return method(query)
        else:
            self.logger.warning(f"지원하지 않는 데이터 유형: {data_type}")
            return []

    def get_detail_by_type(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """데이터 유형별 상세 정보 조회"""
        data_type = item.get('data_type', 'law')

        if data_type == 'ordinance':
            return self.get_ordinance_detail(
                item['law_id'], item.get('law_msn', ''), item['law_name']
            )
        elif data_type == 'precedent':
            return self.get_precedent_detail(item['law_id'], item['law_name'])
        elif data_type == 'constitutional':
            return self.get_constitutional_detail(item['law_id'], item['law_name'])
        elif data_type == 'interpretation':
            return self.get_interpretation_detail(item['law_id'], item['law_name'])
        elif data_type == 'admin_decision':
            return self.get_admin_decision_detail(item['law_id'], item['law_name'])
        elif data_type == 'treaty':
            return self.get_treaty_detail(item['law_id'], item['law_name'])
        else:
            # 기본: 법령/행정규칙
            return self._get_law_detail(
                item['law_id'],
                item.get('law_msn', ''),
                item['law_name'],
                item.get('is_admin_rule', False)
            )


# ===== 법령 내보내기 클래스 =====
class LawExporter:
    """법령 내보내기 클래스 - PDF 지원 수정"""
    
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
    
    def export_to_zip(self, laws_dict: Dict[str, Dict[str, Any]],
                     include_pdfs: bool = False) -> bytes:
        """ZIP 파일로 내보내기 - OCR 텍스트 포함"""
        zip_buffer = BytesIO()

        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            # 메타데이터
            metadata = {
                'collection_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'total_laws': len(laws_dict),
                'admin_rule_count': sum(1 for law in laws_dict.values() if law.get('is_admin_rule', False)),
                'attachment_count': sum(len(law.get('attachments', [])) for law in laws_dict.values()),
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
            readme = self._create_readme(laws_dict, include_pdfs)
            zip_file.writestr('README.md', readme)
        
        zip_buffer.seek(0)
        return zip_buffer.getvalue()

    def export_markdown_by_file(self,
                                grouped_laws: Dict[str, Dict[str, Dict[str, Any]]],
                                file_metadata: Dict[str, Dict[str, Any]]) -> bytes:
        """파일별로 통합된 Markdown 번들을 ZIP으로 반환"""
        zip_buffer = BytesIO()

        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for file_key, laws in grouped_laws.items():
                if not laws:
                    continue

                meta = file_metadata.get(file_key, {})
                file_name = meta.get('file_name') or ("직접_검색" if file_key == 'direct_input' else file_key)
                safe_name = self._sanitize_filename(file_name)
                markdown_content = self._create_all_laws_markdown(laws)
                zip_file.writestr(f'{safe_name}.md', markdown_content)

        zip_buffer.seek(0)
        return zip_buffer.getvalue()
    
    def export_single_file(self, laws_dict: Dict[str, Dict[str, Any]], 
                          format: str = 'json') -> str:
        """단일 파일로 내보내기 - 모든 형식 지원"""
        exporters = {
            'json': self._export_as_json,
            'markdown': self._export_as_markdown,
            'text': self._export_as_text
        }
        
        exporter = exporters.get(format.lower(), self._export_as_json)
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
        
        # 별표/별첨 개수
        if law.get('attachments'):
            lines.append(f"별표/별첨: {len(law['attachments'])}개")
        
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
        
        # 별표/별첨 정보
        if law.get('attachments'):
            lines.append(f"- **별표/별첨**: {len(law['attachments'])}개")
        
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
        admin_rule_count = sum(1 for law in laws_dict.values() if law.get('is_admin_rule', False))
        attachment_count = sum(len(law.get('attachments', [])) for law in laws_dict.values())
        
        if admin_rule_count > 0:
            lines.append(f"**행정규칙 수**: {admin_rule_count}개")
        if attachment_count > 0:
            lines.append(f"**별표/별첨 총계**: {attachment_count}개")
        
        lines.append("")
        
        # 목차
        lines.append("## 📑 목차\n")
        for idx, (law_id, law) in enumerate(laws_dict.items(), 1):
            anchor = self._sanitize_filename(law['law_name'])
            type_emoji = "📋" if law.get('is_admin_rule', False) else "📖"
            attachment_mark = " 📎" if law.get('attachments') else ""
            lines.append(f"{idx}. {type_emoji} [{law['law_name']}](#{anchor}){attachment_mark}")
        lines.append("\n---\n")
        
        # 각 법령
        for law_id, law in laws_dict.items():
            lines.append(self._format_law_markdown(law))
            lines.append("\n---\n")
            
        return '\n'.join(lines)
    
    def _create_readme(self, laws_dict: Dict[str, Dict[str, Any]], 
                      include_pdfs: bool = False) -> str:
        """README 생성"""
        # 통계 계산
        total_articles = sum(len(law.get('articles', [])) for law in laws_dict.values())
        total_provisions = sum(len(law.get('supplementary_provisions', [])) for law in laws_dict.values())
        total_attachments = sum(len(law.get('attachments', [])) for law in laws_dict.values())
        admin_rule_count = sum(1 for law in laws_dict.values() if law.get('is_admin_rule', False))
        
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
            if law.get('is_admin_rule', False):
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
                content += f"- 조문: {len(law.get('articles', []))}개\n"
                if law.get('attachments'):
                    content += f"- 별표/별첨: {len(law['attachments'])}개\n"
                content += "\n"
        
        # 행정규칙 목록
        if admin_rules:
            content += "\n### 📋 행정규칙\n\n"
            for law_id, law in admin_rules:
                content += f"#### {law['law_name']}\n"
                content += f"- 유형: {law.get('law_type', '')}\n"
                if law.get('department'):
                    content += f"- 소관부처: {law.get('department', '')}\n"
                content += f"- 시행일자: {law.get('enforcement_date', '')}\n"
                content += f"- 조문: {len(law.get('articles', []))}개\n"
                if law.get('attachments'):
                    content += f"- 별표/별첨: {len(law['attachments'])}개\n"
                content += "\n"

        return content

    def export_merged_pdf_content(self, laws_dict: Dict[str, Dict[str, Any]],
                                   base_law_name: str = '') -> bytes:
        """여러 법령을 하나의 통합 파일로 내보내기 (PDF 대체용 Markdown)"""
        content = self._create_merged_markdown(laws_dict, base_law_name)
        return content.encode('utf-8')

    def export_merged_markdown(self, laws_dict: Dict[str, Dict[str, Any]],
                                base_law_name: str = '') -> str:
        """여러 법령을 하나의 Markdown 파일로 병합"""
        return self._create_merged_markdown(laws_dict, base_law_name)

    def _create_merged_markdown(self, laws_dict: Dict[str, Dict[str, Any]],
                                 base_law_name: str = '') -> str:
        """통합 Markdown 콘텐츠 생성"""
        lines = []

        # 제목
        title = f"{base_law_name} 법령 체계도" if base_law_name else "법령 통합 문서"
        lines.append(f"# 📚 {title}\n")
        lines.append(f"> 생성일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        lines.append(f"> 총 법령 수: {len(laws_dict)}개\n")

        # 목차 생성
        lines.append("\n---\n")
        lines.append("## 📑 목차\n")

        # 법령을 유형별로 분류
        law_types = {
            '법률': [],
            '시행령': [],
            '시행규칙': [],
            '행정규칙': []
        }

        for law_id, law in laws_dict.items():
            law_name = law.get('law_name', '')
            law_type = law.get('law_type', '')

            if law.get('is_admin_rule') or any(k in law_name for k in ['고시', '훈령', '예규', '규정', '세칙']):
                law_types['행정규칙'].append((law_id, law))
            elif '시행규칙' in law_name or '시행규칙' in law_type:
                law_types['시행규칙'].append((law_id, law))
            elif '시행령' in law_name or '시행령' in law_type:
                law_types['시행령'].append((law_id, law))
            else:
                law_types['법률'].append((law_id, law))

        # 목차 작성
        toc_num = 1
        for type_name, type_laws in law_types.items():
            if type_laws:
                lines.append(f"\n### {type_name}\n")
                for law_id, law in type_laws:
                    # 앵커 링크 생성
                    anchor = self._sanitize_filename(law['law_name']).replace(' ', '-').lower()
                    lines.append(f"{toc_num}. [{law['law_name']}](#{anchor})")
                    toc_num += 1

        lines.append("\n---\n")
        lines.append("## 📖 법령 본문\n")

        # 각 법령 본문 작성
        for type_name, type_laws in law_types.items():
            if type_laws:
                lines.append(f"\n### 📂 {type_name}\n")
                lines.append("---\n")

                for law_id, law in type_laws:
                    lines.append(self._format_law_for_merge(law))
                    lines.append("\n---\n")

        return '\n'.join(lines)

    def _format_law_for_merge(self, law: Dict[str, Any]) -> str:
        """병합 문서용 개별 법령 포맷"""
        lines = []

        # 법령 제목 (앵커 포함)
        anchor = self._sanitize_filename(law['law_name']).replace(' ', '-').lower()
        lines.append(f"<a name=\"{anchor}\"></a>")
        lines.append(f"## 📜 {law['law_name']}\n")

        # 기본 정보 테이블
        lines.append("| 항목 | 내용 |")
        lines.append("|------|------|")
        lines.append(f"| **법종구분** | {law.get('law_type', '-')} |")
        if law.get('department'):
            lines.append(f"| **소관부처** | {law.get('department', '-')} |")
        lines.append(f"| **공포일자** | {law.get('promulgation_date', '-')} |")
        lines.append(f"| **시행일자** | {law.get('enforcement_date', '-')} |")
        if law.get('articles'):
            lines.append(f"| **조문 수** | {len(law['articles'])}개 |")
        if law.get('attachments'):
            lines.append(f"| **별표/별첨** | {len(law['attachments'])}개 |")

        lines.append("")

        # 조문
        if law.get('articles'):
            lines.append("### 📖 조문\n")
            for article in law['articles']:
                lines.append(f"#### {article['number']} {article.get('title', '')}\n")
                lines.append(f"{article['content']}\n")

                if article.get('paragraphs'):
                    for para in article['paragraphs']:
                        lines.append(f"> {para['number']} {para['content']}\n")
                lines.append("")

        # 부칙
        if law.get('supplementary_provisions'):
            lines.append("### 📋 부칙\n")
            for provision in law['supplementary_provisions']:
                if provision.get('promulgation_date'):
                    lines.append(f"#### 부칙 <{provision['promulgation_date']}>\n")
                lines.append(f"{provision['content']}\n")
                lines.append("")

        # 별표/별첨
        if law.get('attachments'):
            lines.append("### 📎 별표/별첨\n")
            for attachment in law['attachments']:
                lines.append(f"#### [{attachment['type']}] {attachment.get('title', '')}\n")
                if attachment.get('content'):
                    # 긴 내용은 접기로 처리
                    content = attachment['content']
                    if len(content) > 500:
                        lines.append("<details>")
                        lines.append("<summary>내용 보기 (클릭하여 펼치기)</summary>\n")
                        lines.append(f"```\n{content}\n```")
                        lines.append("</details>\n")
                    else:
                        lines.append(f"```\n{content}\n```\n")
                lines.append("")

        # 원문 (조문이 없는 경우)
        if not law.get('articles') and law.get('raw_content'):
            lines.append("### 📄 원문\n")
            lines.append(f"```\n{law['raw_content']}\n```\n")

        return '\n'.join(lines)

    def export_merged_zip(self, laws_dict: Dict[str, Dict[str, Any]],
                          base_law_name: str = '') -> bytes:
        """통합 파일과 개별 파일을 모두 포함하는 ZIP 내보내기"""
        zip_buffer = BytesIO()

        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            # 1. 통합 Markdown 파일
            merged_md = self._create_merged_markdown(laws_dict, base_law_name)
            safe_base_name = self._sanitize_filename(base_law_name) if base_law_name else '법령_통합'
            zip_file.writestr(f'{safe_base_name}_통합.md', merged_md)

            # 2. 통합 JSON 파일
            metadata = {
                'collection_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'base_law_name': base_law_name,
                'total_laws': len(laws_dict),
                'laws': laws_dict
            }
            zip_file.writestr(
                f'{safe_base_name}_통합.json',
                json.dumps(metadata, ensure_ascii=False, indent=2)
            )

            # 3. 개별 파일들
            for law_id, law in laws_dict.items():
                safe_name = self._sanitize_filename(law['law_name'])

                # 개별 Markdown
                md_content = self._format_law_markdown(law)
                zip_file.writestr(f'laws/{safe_name}.md', md_content)

                # 개별 JSON
                zip_file.writestr(
                    f'laws/{safe_name}.json',
                    json.dumps(law, ensure_ascii=False, indent=2)
                )

            # 4. README
            readme = self._create_merged_readme(laws_dict, base_law_name)
            zip_file.writestr('README.md', readme)

        zip_buffer.seek(0)
        return zip_buffer.getvalue()

    def _create_merged_readme(self, laws_dict: Dict[str, Dict[str, Any]],
                               base_law_name: str = '') -> str:
        """통합 내보내기용 README 생성"""
        lines = []

        lines.append(f"# 📚 {base_law_name or '법령'} 체계도 수집 결과\n")
        lines.append(f"> 생성일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

        lines.append("## 📊 수집 통계\n")
        lines.append(f"- **총 법령 수**: {len(laws_dict)}개")

        # 유형별 통계
        admin_count = sum(1 for law in laws_dict.values() if law.get('is_admin_rule', False))
        general_count = len(laws_dict) - admin_count
        article_count = sum(len(law.get('articles', [])) for law in laws_dict.values())
        attachment_count = sum(len(law.get('attachments', [])) for law in laws_dict.values())

        lines.append(f"- **일반 법령**: {general_count}개")
        lines.append(f"- **행정규칙**: {admin_count}개")
        lines.append(f"- **총 조문 수**: {article_count}개")
        lines.append(f"- **총 별표/별첨**: {attachment_count}개\n")

        lines.append("## 📁 파일 구조\n")
        lines.append("```")
        safe_base_name = self._sanitize_filename(base_law_name) if base_law_name else '법령_통합'
        lines.append(f"├── {safe_base_name}_통합.md    # 모든 법령을 하나로 통합한 파일")
        lines.append(f"├── {safe_base_name}_통합.json  # 전체 데이터 (JSON)")
        lines.append("├── laws/                       # 개별 법령 파일들")
        lines.append("│   ├── [법령명].md")
        lines.append("│   └── [법령명].json")
        lines.append("└── README.md                   # 이 파일")
        lines.append("```\n")

        lines.append("## 📋 수집된 법령 목록\n")

        for law_id, law in laws_dict.items():
            law_type_icon = "📋" if law.get('is_admin_rule') else "📖"
            lines.append(f"- {law_type_icon} **{law['law_name']}** ({law.get('law_type', '')})")

        return '\n'.join(lines)


# ===== Streamlit UI 함수들 =====
def initialize_session_state():
    """세션 상태 초기화"""
    defaults = {
        'mode': 'direct',
        'extracted_laws': [],
        'file_extractions': {},
        'search_results': [],
        'search_results_by_file': {},
        'selected_laws': [],
        'selected_laws_by_file': {},
        'collected_laws': {},
        'collected_laws_by_file': {},
        'file_processed': False,
        'openai_api_key': None,
        'use_ai': False,
        'oc_code': '',
        'include_pdfs': False,  # PDF 다운로드 옵션
        'current_data_type': 'law'  # 현재 선택된 데이터 유형
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def show_sidebar():
    """사이드바 UI - 개선된 API 키 처리"""
    with st.sidebar:
        st.header("⚙️ 설정")
        
        # 기관코드 입력 - 고유 키 사용
        oc_code = st.text_input(
            "기관코드 (OC)",
            value=st.session_state.get('oc_code', ''),
            placeholder="이메일 @ 앞부분",
            help="예: test@korea.kr → test",
            key="sidebar_oc_code"  # 고유 키 추가
        )
        
        # 값이 변경되면 세션 상태 업데이트
        if oc_code != st.session_state.get('oc_code', ''):
            st.session_state.oc_code = oc_code
        
        st.divider()
        
        # AI 설정 (개선된 버전)
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
                # 현재 API 키 상태 표시
                if st.session_state.get('use_ai', False) and st.session_state.get('openai_api_key'):
                    st.success("✅ AI 기능 활성화됨")
                    st.caption(f"현재 API 키: {st.session_state.openai_api_key[:10]}...")
                    
                    if st.button("🔄 API 키 재설정", type="secondary"):
                        st.session_state.openai_api_key = None
                        st.session_state.use_ai = False
                        st.rerun()
                else:
                    # API 키 입력
                    api_key_input = st.text_input(
                        "OpenAI API Key",
                        type="password",
                        value="",
                        key="openai_key_new_input",  # 고유 키
                        help="https://platform.openai.com/api-keys 에서 발급",
                        placeholder="sk-..."
                    )
                    
                    if st.button("🔑 API 키 설정", type="primary"):
                        if api_key_input:
                            # 키 정리 및 검증
                            cleaned_key = api_key_input.strip()
                            
                            if cleaned_key.startswith(('sk-', 'sess-')) and len(cleaned_key) > 40:
                                with st.spinner("API 키 검증 중..."):
                                    try:
                                        from openai import OpenAI
                                        # 테스트용 클라이언트 생성
                                        test_client = OpenAI(api_key=cleaned_key)
                                        
                                        # 버전 독립적인 API 테스트
                                        success = False
                                        try:
                                            # 가장 간단한 API 호출 - chat completion
                                            test_response = test_client.chat.completions.create(
                                                model="gpt-5",
                                                messages=[{"role": "user", "content": "test"}],
                                                max_tokens=1
                                            )
                                            success = True
                                        except Exception as chat_error:
                                            # chat API 실패 시 models API 시도
                                            try:
                                                # 신버전 API 호환
                                                test_response = test_client.models.list()
                                                if test_response and hasattr(test_response, 'data'):
                                                    success = True
                                            except:
                                                # gpt-5로 재시도
                                                try:
                                                    test_response = test_client.chat.completions.create(
                                                        model="gpt-5",
                                                        messages=[{"role": "user", "content": "test"}],
                                                        max_tokens=1
                                                    )
                                                    success = True
                                                except:
                                                    success = False
                                        
                                        if success:
                                            # 성공하면 세션에 저장
                                            st.session_state.openai_api_key = cleaned_key
                                            st.session_state.use_ai = True
                                            st.success("✅ API 키가 검증되었습니다!")
                                            logger.info(f"API 키 설정 및 검증 완료")
                                            st.rerun()
                                        else:
                                            raise Exception("API 키 검증 실패")
                                        
                                    except Exception as e:
                                        error_msg = str(e)
                                        if "401" in error_msg or "Incorrect API key" in error_msg:
                                            st.error("❌ API 키가 유효하지 않습니다.")
                                            st.info("올바른 OpenAI API 키인지 확인해주세요.")
                                        elif "429" in error_msg:
                                            st.warning("⚠️ API 사용 한도 초과. 나중에 다시 시도해주세요.")
                                        else:
                                            st.error(f"❌ API 키 검증 실패: {error_msg}")
                                        
                                        logger.error(f"API 키 검증 실패: {e}")
                            else:
                                st.error("❌ 올바른 형식의 API 키가 아닙니다.")
                                st.info("'sk-' 또는 'sess-'로 시작하는 키를 입력해주세요.")
                        else:
                            st.warning("API 키를 입력해주세요.")
        
        st.divider()
        
        # PDF/OCR 옵션
        st.subheader("📄 별표/별첨 처리")
        st.info("별표/별첨은 텍스트로 자동 수집됩니다.")
        st.caption("PDF로만 제공되는 경우 수집 후 OCR 처리 가능")
        
        st.divider()
        
        # 모드 선택
        st.subheader("🎯 수집 방식")
        mode = st.radio(
            "방식 선택",
            ["직접 검색", "파일 업로드"],
            help="직접 검색: 법령명을 입력하여 검색\n파일 업로드: 파일에서 법령 추출",
            key="sidebar_mode"  # 고유 키
        )
        st.session_state.mode = 'direct' if mode == "직접 검색" else 'file'
        
        # 테스트 버튼 추가
        st.divider()
        st.subheader("🧪 테스트")
        
        if st.button("행정규칙 검색 테스트", type="secondary", use_container_width=True):
            if not st.session_state.get('oc_code', ''):
                st.error("기관코드를 먼저 입력해주세요!")
            else:
                test_admin_rule_search(st.session_state.oc_code)
        
        # 초기화 버튼
        if st.button("🔄 초기화", type="secondary", use_container_width=True):
            # 유지할 키 목록 - 기관코드와 API 키 추가
            keys_to_keep = ['mode', 'oc_code', 'openai_api_key', 'use_ai']
            for key in list(st.session_state.keys()):
                if key not in keys_to_keep:
                    del st.session_state[key]
            st.rerun()
        
        return st.session_state.get('oc_code', '')


def test_admin_rule_search(oc_code: str):
    """행정규칙 검색 테스트 - PDF 디버깅 정보 추가"""
    with st.spinner("행정규칙 검색 테스트 중..."):
        collector = LawCollectorAPI(oc_code)
        
        # 테스트할 행정규칙들
        test_rules = [
            "금융기관검사및제재에관한규정",
            "여신전문금융업감독규정",
            "여신전문금융업감독업무시행세칙"
        ]
        
        for rule_name in test_rules:
            st.write(f"\n🔍 검색 중: {rule_name}")
            
            # 실제 검색 메서드 테스트
            found = collector.search_single_law(rule_name)
            if found:
                st.success(f"✅ {len(found)}개 발견!")
                for item in found:
                    type_emoji = "📋" if item.get('is_admin_rule') else "📖"
                    st.write(f"    {type_emoji} {item['law_name']} ({item['law_type']})")
                    
                    # 별표/별첨 확인
                    if found:
                        with st.expander(f"상세 테스트: {item['law_name']}"):
                            detail = collector._get_law_detail(
                                item['law_id'], 
                                item['law_msn'], 
                                item['law_name'], 
                                item.get('is_admin_rule', False)
                            )
                            if detail:
                                if detail.get('attachments'):
                                    st.info(f"📎 별표/별지: {len(detail['attachments'])}개")
                                    
                                    # 별표/별지 정보 표시
                                    for att in detail['attachments']:
                                        st.write(f"**{att['type']} {att.get('number', '')}**: {att.get('title', '')}")
                                        if att.get('content'):
                                            st.text(f"내용 길이: {len(att['content'])}자")
                                else:
                                    st.warning("📎 별표/별지 없음")
            else:
                st.warning(f"❌ 검색 결과 없음")
            
            time.sleep(0.5)


def handle_hierarchy_search(collector: LawCollectorAPI, query: str):
    """법령 체계도 검색 처리 - 상하위법 일괄 검색"""
    st.subheader(f"📊 '{query}' 법령 체계도 검색")

    # 진행 상태 표시
    progress_bar = st.progress(0)
    status_text = st.empty()

    def update_progress(progress: float, message: str):
        progress_bar.progress(progress)
        status_text.text(message)

    # 체계도 기반 검색 실행
    hierarchy_result = collector.search_with_hierarchy(query, update_progress)

    # 진행 상태 정리
    progress_bar.empty()
    status_text.empty()

    # 결과가 없는 경우
    if not hierarchy_result.get('laws'):
        st.warning("검색 결과가 없습니다.")
        st.info("💡 Tip: 다른 법령명으로 검색해 보세요.")
        return

    # 폴백 여부 표시
    if hierarchy_result.get('fallback'):
        st.info("ℹ️ 체계도 정보가 없어 일반 검색 결과를 표시합니다.")

    # 체계도 정보 표시
    hierarchy_info = hierarchy_result.get('hierarchy_info')
    if hierarchy_info:
        with st.expander("📊 법령 체계도 구조", expanded=True):
            # 기본 정보
            st.markdown(f"**기준 법령:** {hierarchy_info.get('law_name', query)}")
            st.markdown(f"**법종:** {hierarchy_info.get('law_type', '-')}")

            # 관련 법령 구조
            related = hierarchy_info.get('related_laws', {})

            col1, col2 = st.columns(2)

            with col1:
                st.markdown("**📜 법률**")
                laws = related.get('laws', [])
                if laws:
                    for law in laws:
                        st.write(f"  • {law.get('name', '')}")
                else:
                    st.write("  (없음)")

                st.markdown("**📋 시행령**")
                decrees = related.get('enforcement_decrees', [])
                if decrees:
                    for decree in decrees:
                        st.write(f"  • {decree.get('name', '')}")
                else:
                    st.write("  (없음)")

            with col2:
                st.markdown("**📑 시행규칙**")
                rules = related.get('enforcement_rules', [])
                if rules:
                    for rule in rules:
                        st.write(f"  • {rule.get('name', '')}")
                else:
                    st.write("  (없음)")

                st.markdown("**📌 행정규칙 (고시/훈령 등)**")
                admin_rules = related.get('admin_rules', [])
                if admin_rules:
                    for admin in admin_rules:
                        st.write(f"  • {admin.get('name', '')} [{admin.get('type', '')}]")
                else:
                    st.write("  (없음)")

    # 검색 결과 요약
    summary = hierarchy_result.get('search_summary', {})
    st.success(f"✅ 총 {summary.get('total', 0)}개의 법령을 찾았습니다!")

    # 통계 표시
    stats_cols = st.columns(4)
    with stats_cols[0]:
        st.metric("법률", summary.get('laws_count', 0))
    with stats_cols[1]:
        st.metric("시행령", summary.get('decrees_count', 0))
    with stats_cols[2]:
        st.metric("시행규칙", summary.get('rules_count', 0))
    with stats_cols[3]:
        st.metric("행정규칙", summary.get('admin_rules_count', 0))

    # 결과를 세션에 저장
    results = hierarchy_result.get('laws', [])
    st.session_state.search_results = results
    st.session_state.current_data_type = 'hierarchy'
    st.session_state.hierarchy_info = hierarchy_info

    # 결과 목록 표시
    st.subheader("📋 검색된 법령 목록")

    # 전체 선택 옵션
    select_all = st.checkbox("전체 선택", value=True, key="hierarchy_select_all")

    # 결과 표시 및 선택
    selected_laws = []

    # 유형별로 그룹화하여 표시
    law_groups = {
        '법률': [],
        '시행령': [],
        '시행규칙': [],
        '행정규칙': []
    }

    for law in results:
        law_name = law.get('law_name', '')
        law_type = law.get('law_type', '')

        if law.get('is_admin_rule') or any(k in law_name for k in ['고시', '훈령', '예규', '규정', '세칙']):
            law_groups['행정규칙'].append(law)
        elif '시행규칙' in law_name or '시행규칙' in law_type:
            law_groups['시행규칙'].append(law)
        elif '시행령' in law_name or '시행령' in law_type:
            law_groups['시행령'].append(law)
        else:
            law_groups['법률'].append(law)

    # 그룹별로 표시
    for group_name, group_laws in law_groups.items():
        if group_laws:
            with st.expander(f"{group_name} ({len(group_laws)}개)", expanded=True):
                for idx, law in enumerate(group_laws):
                    col1, col2, col3 = st.columns([0.5, 5, 2])

                    with col1:
                        is_selected = st.checkbox(
                            "",
                            value=select_all,
                            key=f"hierarchy_law_{law['law_id']}_{idx}",
                            label_visibility="collapsed"
                        )
                        if is_selected:
                            selected_laws.append(law)

                    with col2:
                        st.write(f"**{law.get('law_name', '')}**")
                        if law.get('hierarchy_source'):
                            st.caption(f"체계도 출처: {law['hierarchy_source']}")

                    with col3:
                        law_type = law.get('law_type', '')
                        if law.get('is_admin_rule'):
                            st.caption(f"🏛️ 행정규칙 | {law_type}")
                        else:
                            st.caption(f"📜 {law_type}")

    # 선택된 법령 저장
    st.session_state.hierarchy_selected_laws = selected_laws

    # 수집 버튼
    st.divider()

    if st.button("📥 선택한 법령 상세 정보 수집", type="primary", use_container_width=True):
        if not selected_laws:
            st.error("수집할 법령을 선택해주세요!")
        else:
            collect_hierarchy_laws(collector, selected_laws)


def collect_hierarchy_laws(collector: LawCollectorAPI, laws: List[Dict[str, Any]]):
    """체계도에서 선택한 법령들의 상세 정보 수집"""
    st.subheader("📥 법령 상세 정보 수집 중...")

    progress_bar = st.progress(0)
    status_text = st.empty()

    collected_details = {}
    errors = []

    for idx, law in enumerate(laws):
        progress = (idx + 1) / len(laws)
        progress_bar.progress(progress)
        status_text.text(f"수집 중: {law.get('law_name', '')} ({idx + 1}/{len(laws)})")

        try:
            # 상세 정보 조회
            detail = collector.get_detail_by_type(law)

            if detail:
                collected_details[law['law_id']] = detail
            else:
                errors.append(law.get('law_name', ''))

        except Exception as e:
            logger.error(f"법령 수집 오류: {law.get('law_name', '')}: {e}")
            errors.append(law.get('law_name', ''))

        time.sleep(0.2)  # API 부하 방지

    progress_bar.empty()
    status_text.empty()

    # 결과 저장
    st.session_state.collected_laws = collected_details

    # 결과 표시
    st.success(f"✅ {len(collected_details)}개 법령의 상세 정보를 수집했습니다!")

    if errors:
        with st.expander(f"⚠️ 수집 실패 ({len(errors)}개)"):
            for err in errors:
                st.write(f"- {err}")

    # 통계 표시
    total_articles = sum(len(d.get('articles', [])) for d in collected_details.values())
    total_attachments = sum(len(d.get('attachments', [])) for d in collected_details.values())

    stats_cols = st.columns(3)
    with stats_cols[0]:
        st.metric("수집 법령", len(collected_details))
    with stats_cols[1]:
        st.metric("조문 수", total_articles)
    with stats_cols[2]:
        st.metric("별표/별지", total_attachments)


def handle_direct_search_mode(oc_code: str):
    """직접 검색 모드 처리"""
    st.header("🔍 직접 검색 모드")

    # 데이터 유형 선택
    st.subheader("📂 데이터 유형 선택")

    data_type_options = {
        "📊 법령 체계도 (상하위법 일괄)": "hierarchy",
        "법령/행정규칙": "law",
        "자치법규": "ordinance",
        "판례": "precedent",
        "헌재결정례": "constitutional",
        "법령해석례": "interpretation",
        "행정심판례": "admin_decision",
        "조약": "treaty"
    }

    selected_type_label = st.selectbox(
        "검색할 데이터 유형",
        options=list(data_type_options.keys()),
        index=0,
        help="검색할 법률 데이터 유형을 선택하세요 (기본: 법령 체계도 검색)"
    )

    selected_data_type = data_type_options[selected_type_label]

    # 데이터 유형별 설명
    type_descriptions = {
        "law": "💡 법령/행정규칙 검색: 띄어쓰기 변형을 포함하여 최대한 많은 법령을 찾습니다.",
        "hierarchy": "📊 법령 체계도 검색: 상위법(법률)을 검색하면 시행령, 시행규칙, 행정규칙(고시, 훈령 등) 등 관련된 모든 하위법령을 함께 검색합니다.",
        "ordinance": "📜 자치법규 검색: 지방자치단체의 조례, 규칙 등을 검색합니다.",
        "precedent": "⚖️ 판례 검색: 대법원 및 하급법원 판례를 검색합니다.",
        "constitutional": "🏛️ 헌재결정례 검색: 헌법재판소의 결정례를 검색합니다.",
        "interpretation": "📖 법령해석례 검색: 법제처 등의 법령 해석 사례를 검색합니다.",
        "admin_decision": "📋 행정심판례 검색: 행정심판위원회의 재결례를 검색합니다.",
        "treaty": "🌐 조약 검색: 국제 조약 및 협정을 검색합니다."
    }

    st.info(type_descriptions.get(selected_data_type, ""))

    # 검색어 입력
    placeholder_texts = {
        "law": "예: 민법, 상법, 금융감독규정",
        "hierarchy": "예: 금융지주회사법, 자본시장법, 개인정보보호법",
        "ordinance": "예: 주차장, 환경, 청소년",
        "precedent": "예: 손해배상, 계약해제",
        "constitutional": "예: 위헌, 기본권",
        "interpretation": "예: 임대차, 건축",
        "admin_decision": "예: 영업정지, 허가취소",
        "treaty": "예: 무역, 투자, 인권"
    }

    search_query = st.text_input(
        "검색어",
        placeholder=placeholder_texts.get(selected_data_type, "검색어를 입력하세요"),
        help="검색할 키워드를 입력하세요"
    )

    if st.button("🔍 검색", type="primary", use_container_width=True):
        if not oc_code:
            st.error("기관코드를 입력해주세요!")
        elif not search_query:
            st.error("검색어를 입력해주세요!")
        else:
            with st.spinner(f"'{search_query}' 검색 중... ({selected_type_label})"):
                collector = LawCollectorAPI(oc_code)

                # 데이터 유형에 따른 검색
                if selected_data_type == "hierarchy":
                    # 법령 체계도 검색 (상하위법 일괄 검색)
                    handle_hierarchy_search(collector, search_query)
                    return  # 체계도 검색은 별도 처리
                elif selected_data_type == "law":
                    # 기존 법령/행정규칙 검색 (변형 검색 포함)
                    results = collector._search_with_variations(search_query)
                else:
                    # 새로운 데이터 유형 검색
                    results = collector.search_by_type(search_query, selected_data_type)

                if results:
                    st.success(f"{len(results)}개의 결과를 찾았습니다!")

                    # 결과 유형별 정보 표시
                    if selected_data_type == "law":
                        admin_count = sum(1 for r in results if r.get('is_admin_rule'))
                        if admin_count > 0:
                            st.info(f"📋 이 중 {admin_count}개는 행정규칙입니다.")

                        # 검색 변형 표시
                        variations_used = set()
                        for r in results:
                            if 'found_with_variation' in r:
                                variations_used.add(r['found_with_variation'])

                        if variations_used and len(variations_used) > 1:
                            with st.expander("🔍 검색에 사용된 변형"):
                                for var in variations_used:
                                    st.write(f"- {var}")

                    st.session_state.search_results = results
                    st.session_state.current_data_type = selected_data_type
                else:
                    st.warning("검색 결과가 없습니다.")
                    st.info("💡 Tip: 다른 키워드로 검색하거나 기관코드를 확인해보세요.")
                    st.session_state.search_results = []


def handle_file_upload_mode(oc_code: str):
    """파일 업로드 모드 처리"""
    st.header("📄 파일 업로드 모드")
    
    # AI 상태 표시 (수정)
    if st.session_state.use_ai and st.session_state.openai_api_key:
        st.info(f"🤖 AI 강화 모드 활성화")
    else:
        st.info("💡 AI 설정을 통해 법령명 추출 정확도를 높일 수 있습니다")
    
    uploaded_files = st.file_uploader(
        "파일 선택",
        type=['pdf', 'xlsx', 'xls', 'md', 'txt'],
        help="PDF, Excel, Markdown, 텍스트 파일을 지원합니다",
        accept_multiple_files=True
    )

    if uploaded_files:
        st.subheader("📋 STEP 1: 법령명 추출")

        extractor = EnhancedLawFileExtractor(
            use_ai=st.session_state.use_ai,
            api_key=st.session_state.openai_api_key
        )

        newly_processed = []

        for uploaded_file in uploaded_files:
            file_type = uploaded_file.name.split('.')[-1].lower()
            file_key = f"{uploaded_file.name}_{uploaded_file.size}"

            if file_key in st.session_state.file_extractions:
                continue

            with st.spinner(f"'{uploaded_file.name}'에서 법령명을 추출하는 중..."):
                try:
                    uploaded_file.seek(0)
                    extracted_laws = extractor.extract_from_file(uploaded_file, file_type)

                    st.session_state.file_extractions[file_key] = {
                        'file_name': uploaded_file.name,
                        'file_type': file_type,
                        'laws': extracted_laws,
                        'edited_laws': extracted_laws.copy()
                    }

                    newly_processed.append((uploaded_file.name, len(extracted_laws)))

                except Exception as e:
                    st.error(f"파일 처리 오류 ({uploaded_file.name}): {str(e)}")
                    logger.error(f"파일 처리 오류: {e}", exc_info=True)

        if newly_processed:
            for name, count in newly_processed:
                if count:
                    st.success(f"✅ {name}: {count}개의 법령명을 찾았습니다!")
                else:
                    st.warning(f"⚠️ {name}: 법령명을 찾지 못했습니다")

        st.session_state.file_processed = bool(st.session_state.file_extractions)

        # 전체 리스트도 유지 (기존 기능 호환)
        st.session_state.extracted_laws = [
            law
            for data in st.session_state.file_extractions.values()
            for law in data.get('edited_laws', [])
        ]

    # 추출된 법령 표시
    if st.session_state.file_extractions:
        display_extracted_laws(oc_code)


def display_extracted_laws(oc_code: str):
    """추출된 법령 표시 및 편집"""
    st.subheader("✏️ STEP 2: 법령명 확인 및 편집")

    file_extractions = st.session_state.file_extractions
    removal_queue = []

    total_law_count = 0
    total_admin_count = 0

    for file_key, data in file_extractions.items():
        laws_for_file = data.get('edited_laws', [])
        total_law_count += len(laws_for_file)
        total_admin_count += sum(1 for law in laws_for_file
                                 if any(k in law for k in LawPatterns.ADMIN_KEYWORDS))

        with st.expander(f"📄 {data['file_name']} ({len(laws_for_file)}개)", expanded=True):
            st.caption("한 줄에 하나씩 법령명을 입력하거나 수정할 수 있습니다.")

            default_text = "\n".join(laws_for_file)
            edited_text = st.text_area(
                "법령명 목록",
                value=default_text,
                height=200,
                key=f"law_area_{file_key}"
            )

            updated_laws = [line.strip() for line in edited_text.split('\n') if line.strip()]
            st.session_state.file_extractions[file_key]['edited_laws'] = updated_laws

            col_a, col_b = st.columns([3, 1])
            with col_a:
                new_law = st.text_input(
                    "새 법령명 추가",
                    key=f"new_law_{file_key}"
                )
                if st.button("➕ 추가", key=f"add_btn_{file_key}"):
                    if new_law and new_law.strip():
                        updated_laws.append(new_law.strip())
                        st.session_state.file_extractions[file_key]['edited_laws'] = updated_laws
                        st.success(f"'{new_law.strip()}'을(를) 추가했습니다")
                        st.session_state.extracted_laws = [
                            law
                            for item in st.session_state.file_extractions.values()
                            for law in item.get('edited_laws', [])
                        ]
                        st.experimental_rerun()
                    else:
                        st.warning("추가할 법령명을 입력해주세요")

            with col_b:
                st.metric("법령 수", len(updated_laws))
                if st.button("🗑️ 파일 제거", key=f"remove_{file_key}"):
                    removal_queue.append(file_key)

    if removal_queue:
        for key in removal_queue:
            st.session_state.file_extractions.pop(key, None)
        # 관련 상태 정리
        for state_key in ['search_results_by_file', 'selected_laws_by_file', 'collected_laws_by_file']:
            if state_key in st.session_state:
                for key in removal_queue:
                    if key in st.session_state[state_key]:
                        st.session_state[state_key].pop(key, None)

        # 전체 리스트 갱신
        st.session_state.extracted_laws = [
            law
            for item in st.session_state.file_extractions.values()
            for law in item.get('edited_laws', [])
        ]

        st.experimental_rerun()

    summary_col1, summary_col2 = st.columns(2)
    with summary_col1:
        st.metric("총 법령", total_law_count)
    with summary_col2:
        st.metric("추정 행정규칙", total_admin_count)

    st.session_state.extracted_laws = [
        law
        for item in st.session_state.file_extractions.values()
        for law in item.get('edited_laws', [])
    ]

    # 검색 버튼
    if st.button("🔍 모든 파일에서 법령 검색", type="primary", use_container_width=True):
        if not oc_code:
            st.error("기관코드를 입력해주세요!")
        else:
            # 파일별 검색 요청 생성
            law_requests = []
            for file_key, data in st.session_state.file_extractions.items():
                for order, law_name in enumerate(data.get('edited_laws', [])):
                    law_requests.append({
                        'file_key': file_key,
                        'file_name': data['file_name'],
                        'law_name': law_name,
                        'order': order
                    })

            if law_requests:
                st.session_state.search_requests = law_requests
                search_laws_from_list(oc_code, law_requests, is_from_file=True)
            else:
                st.warning("검색할 법령명이 없습니다. 목록을 확인해주세요.")


def search_laws_from_list(oc_code: str, law_inputs: List[Any], is_from_file: bool = True):
    """파일 또는 입력에서 수집한 법령명을 검색"""

    collector = LawCollectorAPI(oc_code)

    law_requests: Optional[List[Dict[str, Any]]] = None
    law_names: List[str] = []

    if law_inputs:
        if isinstance(law_inputs[0], dict):
            law_requests = [cast(Dict[str, Any], item) for item in law_inputs]  # type: ignore[misc]
            law_names = [req['law_name'] for req in law_requests]
        else:
            law_names = [str(name) for name in law_inputs]

    if not law_names:
        st.warning("검색할 법령명이 없습니다.")
        st.session_state.search_results = []
        st.session_state.search_results_by_file = {}
        return None

    progress_bar = st.progress(0)
    status_text = st.empty()

    def update_progress(progress):
        progress_bar.progress(progress)

    if is_from_file:
        st.info("📋 법령체계도 모드: 추출된 법령명과 정확히 일치하는 법령만 검색합니다.")
    else:
        st.info("🔍 직접 검색 모드: 띄어쓰기 변형 등을 포함하여 포괄적으로 검색합니다.")

    with st.spinner("법령을 검색하는 중..."):
        results = collector.search_laws(
            law_names,
            progress_callback=update_progress,
            use_variations=(not is_from_file)
        )

    progress_bar.progress(1.0)
    status_text.text("검색 완료!")

    results_by_file: Dict[str, List[Dict[str, Any]]] = {}

    if law_requests:
        request_map: Dict[str, List[Dict[str, Any]]] = {}
        for req in law_requests:
            request_map.setdefault(req['law_name'], []).append(req)

        results_by_query: Dict[str, List[Dict[str, Any]]] = {}
        for result in results:
            results_by_query.setdefault(result['search_query'], []).append(result)

        for law_name, requests in request_map.items():
            matches = results_by_query.get(law_name, [])
            if not matches:
                continue

            unique_matches: Dict[str, Dict[str, Any]] = {}
            for match in matches:
                unique_matches.setdefault(match['law_id'], match)

            for req in requests:
                bucket = results_by_file.setdefault(req['file_key'], [])
                for match in unique_matches.values():
                    law_copy = match.copy()
                    law_copy['source_file_key'] = req['file_key']
                    law_copy['source_file_name'] = req['file_name']
                    law_copy['source_law_name'] = req['law_name']
                    law_copy['source_order'] = req['order']
                    bucket.append(law_copy)

        results = [
            law
            for laws in results_by_file.values()
            for law in laws
        ]

    st.session_state.search_results_by_file = results_by_file

    if results:
        st.success(f"✅ 총 {len(results)}개의 법령을 찾았습니다!")

        admin_count = sum(1 for r in results if r.get('is_admin_rule'))
        if admin_count > 0:
            st.info(f"📋 이 중 {admin_count}개는 행정규칙입니다.")

        if is_from_file:
            with st.expander("💡 검색 모드 정보"):
                st.write("**법령체계도 모드**에서는 다음과 같이 작동합니다:")
                st.write("- ✅ 추출된 법령명과 정확히 일치하는 법령만 검색")
                st.write("- ❌ 띄어쓰기 변형이나 유사 법령명 검색하지 않음")
                st.write("- 💡 법령체계도에 명시된 법령만 수집하여 정확성 보장")

        st.session_state.search_results = results

        if law_requests:
            for file_key, laws in results_by_file.items():
                file_name = next(
                    (req['file_name'] for req in law_requests if req['file_key'] == file_key),
                    file_key
                )
                st.caption(f"📁 {file_name}: {len(laws)}건 검색")
    else:
        st.session_state.search_results = []
        st.warning("검색 결과가 없습니다")

    return None


def get_data_type_emoji(law: Dict[str, Any]) -> str:
    """데이터 유형에 따른 이모지 반환"""
    data_type = law.get('data_type', '')
    type_emojis = {
        'ordinance': '📜',
        'precedent': '⚖️',
        'constitutional': '🏛️',
        'interpretation': '📖',
        'admin_decision': '📋',
        'treaty': '🌐'
    }

    if data_type in type_emojis:
        return type_emojis[data_type]
    elif law.get('is_admin_rule'):
        return '📋'
    else:
        return '📖'


def display_search_results_and_collect(oc_code: str):
    """검색 결과 표시 및 수집"""
    results_by_file = st.session_state.get('search_results_by_file', {})

    if not st.session_state.search_results and not any(results_by_file.values()):
        return

    st.subheader("📑 검색 결과")

    selected_laws_by_file: Dict[str, List[Dict[str, Any]]] = {}

    if results_by_file:
        for file_key, laws in results_by_file.items():
            if not laws:
                continue

            file_name = st.session_state.file_extractions.get(file_key, {}).get('file_name', file_key)
            st.markdown(f"### 📄 {file_name}")

            cols = st.columns([1, 1, 3, 2, 2])
            headers = ["선택", "유형", "법령명", "법종구분", "검색어"]
            for col, header in zip(cols, headers):
                col.markdown(f"**{header}**")

            select_all_file = st.checkbox("전체 선택", key=f"select_all_{file_key}")

            file_selected: List[Dict[str, Any]] = []
            for idx, law in enumerate(laws):
                row_cols = st.columns([1, 1, 3, 2, 2])

                with row_cols[0]:
                    if st.checkbox(
                        "선택",
                        key=f"sel_{file_key}_{idx}",
                        value=select_all_file,
                        label_visibility="collapsed"
                    ):
                        file_selected.append(law)

                with row_cols[1]:
                    st.write(get_data_type_emoji(law))

                with row_cols[2]:
                    st.write(law['law_name'])

                with row_cols[3]:
                    st.write(law.get('law_type', ''))

                with row_cols[4]:
                    st.write(law.get('source_law_name', law.get('search_query', '')))

            selected_laws_by_file[file_key] = file_selected

            st.divider()
    else:
        # 직접 검색 모드 (파일 없음)용 기존 테이블 유지
        current_data_type = st.session_state.get('current_data_type', 'law')

        select_all = st.checkbox("전체 선택")

        # 데이터 유형에 따라 헤더 조정
        if current_data_type == 'precedent':
            cols = st.columns([1, 1, 3, 2, 2, 2])
            headers = ["선택", "유형", "사건명", "법원", "선고일자", "사건번호"]
        elif current_data_type == 'constitutional':
            cols = st.columns([1, 1, 3, 2, 2])
            headers = ["선택", "유형", "사건명", "종국일자", "사건번호"]
        elif current_data_type == 'interpretation':
            cols = st.columns([1, 1, 3, 2, 2])
            headers = ["선택", "유형", "안건명", "회신일자", "회신기관"]
        elif current_data_type == 'admin_decision':
            cols = st.columns([1, 1, 3, 2, 2])
            headers = ["선택", "유형", "사건명", "의결일자", "재결구분"]
        elif current_data_type == 'treaty':
            cols = st.columns([1, 1, 3, 2, 2])
            headers = ["선택", "유형", "조약명", "발효일자", "체결국가"]
        elif current_data_type == 'ordinance':
            cols = st.columns([1, 1, 3, 2, 2])
            headers = ["선택", "유형", "자치법규명", "시행일자", "자치단체"]
        else:
            cols = st.columns([1, 1, 3, 2, 2, 2])
            headers = ["선택", "유형", "법령명", "법종구분", "시행일자", "검색어"]

        for col, header in zip(cols, headers):
            col.markdown(f"**{header}**")

        st.divider()

        selected_indices = []
        for idx, law in enumerate(st.session_state.search_results):
            if current_data_type == 'precedent':
                row_cols = st.columns([1, 1, 3, 2, 2, 2])
            elif current_data_type in ['constitutional', 'interpretation', 'admin_decision', 'treaty', 'ordinance']:
                row_cols = st.columns([1, 1, 3, 2, 2])
            else:
                row_cols = st.columns([1, 1, 3, 2, 2, 2])

            with row_cols[0]:
                if st.checkbox(
                    "선택",
                    key=f"sel_direct_{idx}",
                    value=select_all,
                    label_visibility="collapsed"
                ):
                    selected_indices.append(idx)

            with row_cols[1]:
                st.write(get_data_type_emoji(law))

            with row_cols[2]:
                st.write(law['law_name'])

            # 데이터 유형에 따라 다른 필드 표시
            if current_data_type == 'precedent':
                with row_cols[3]:
                    st.write(law.get('court', ''))
                with row_cols[4]:
                    st.write(law.get('decision_date', ''))
                with row_cols[5]:
                    st.write(law.get('case_no', ''))
            elif current_data_type == 'constitutional':
                with row_cols[3]:
                    st.write(law.get('decision_date', ''))
                with row_cols[4]:
                    st.write(law.get('case_no', ''))
            elif current_data_type == 'interpretation':
                with row_cols[3]:
                    st.write(law.get('reply_date', ''))
                with row_cols[4]:
                    st.write(law.get('reply_org', ''))
            elif current_data_type == 'admin_decision':
                with row_cols[3]:
                    st.write(law.get('decision_date', ''))
                with row_cols[4]:
                    st.write(law.get('decision_type', ''))
            elif current_data_type == 'treaty':
                with row_cols[3]:
                    st.write(law.get('enforcement_date', ''))
                with row_cols[4]:
                    st.write(law.get('country', ''))
            elif current_data_type == 'ordinance':
                with row_cols[3]:
                    st.write(law.get('enforcement_date', ''))
                with row_cols[4]:
                    st.write(law.get('local_gov', ''))
            else:
                with row_cols[3]:
                    st.write(law.get('law_type', ''))
                with row_cols[4]:
                    st.write(law.get('enforcement_date', ''))
                with row_cols[5]:
                    st.write(law.get('search_query', ''))

        direct_selection = [st.session_state.search_results[i] for i in selected_indices]
        if direct_selection:
            selected_laws_by_file['direct_input'] = direct_selection

    st.session_state.selected_laws_by_file = selected_laws_by_file

    flattened_selected = [
        law
        for laws in selected_laws_by_file.values()
        for law in laws
    ]
    st.session_state.selected_laws = flattened_selected

    if flattened_selected:
        st.success(f"{len(flattened_selected)}개 항목이 선택되었습니다")

        # 유형별 통계 표시
        type_counts = {}
        for law in flattened_selected:
            data_type = law.get('data_type', 'law')
            law_type = law.get('law_type', '기타')
            key = f"{get_data_type_emoji(law)} {law_type}"
            type_counts[key] = type_counts.get(key, 0) + 1

        if len(type_counts) > 1 or (len(type_counts) == 1 and list(type_counts.values())[0] > 0):
            type_info = ", ".join([f"{k}: {v}개" for k, v in type_counts.items()])
            st.info(type_info)

        if st.button("📥 선택한 항목 수집", type="primary", use_container_width=True):
            collect_selected_laws(oc_code)


def collect_selected_laws(oc_code: str):
    """선택된 법령 수집 - PDF 다운로드 제거, 텍스트 내용 활용"""
    collector = LawCollectorAPI(oc_code)
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    def update_progress(progress):
        progress_bar.progress(progress)

    with st.spinner("법령 상세 정보를 수집하는 중..."):
        selected_by_file = st.session_state.get('selected_laws_by_file', {})
        expand_hierarchy = 'direct_input' in selected_by_file
        collected = collector.collect_law_details(
            st.session_state.selected_laws,
            progress_callback=update_progress,
            expand_hierarchy=expand_hierarchy
        )

    def _law_key(law: Dict[str, Any]) -> Tuple[str, str]:
        return (law.get('law_id') or '', law.get('law_msn') or '')

    selected_keys = {_law_key(law) for law in st.session_state.selected_laws}
    auto_added_ids = [
        law_id
        for law_id, detail in collected.items()
        if (law_id, detail.get('law_msn') or '') not in selected_keys
    ]

    if auto_added_ids:
        st.success(f"법령 체계 확장으로 {len(auto_added_ids)}개의 관련 법령을 추가로 수집했습니다.")
        with st.expander("자동으로 추가된 법령 확인"):
            for law_id in auto_added_ids:
                law_detail = collected[law_id]
                relation = law_detail.get('relationship_from_parent', '관련 법령')
                emoji = "📋" if law_detail.get('is_admin_rule') else "📖"
                st.write(f"{emoji} {law_detail['law_name']} ({relation})")

    # 별표/별첨 정보 표시
    total_attachments = sum(len(law.get('attachments', [])) for law in collected.values())
    if total_attachments > 0:
        st.info(f"📎 총 {total_attachments}개의 별표/별첨을 찾았습니다.")
        
        # PDF 대신 텍스트 내용 활용 안내
        with st.expander("📄 별표/별첨 처리 안내"):
            st.write("**별표/별첨 내용 처리 방법:**")
            st.write("1. 텍스트로 제공되는 내용은 자동으로 수집됩니다.")
            st.write("2. PDF로만 제공되는 경우:")
            st.write("   - 법제처 사이트에서 직접 다운로드")
            st.write("   - 다운로드한 PDF를 아래에서 업로드하여 OCR 처리")
            
            # PDF 업로드 및 OCR 처리
            st.subheader("📤 PDF 파일 업로드 (OCR 처리)")
            uploaded_pdfs = st.file_uploader(
                "별표/별첨 PDF 파일을 업로드하세요",
                type=['pdf'],
                accept_multiple_files=True,
                help="법제처에서 다운로드한 별표/별첨 PDF를 업로드하면 OCR로 텍스트를 추출합니다."
            )
            
            if uploaded_pdfs:
                for pdf_file in uploaded_pdfs:
                    with st.spinner(f"{pdf_file.name} OCR 처리 중..."):
                        try:
                            # OCR로 텍스트 추출
                            text = extract_text_from_pdf(pdf_file)
                            if text:
                                st.success(f"✅ {pdf_file.name}: {len(text)}자 추출 완료")
                                
                                # 추출된 텍스트를 해당 법령에 추가
                                # PDF 파일명에서 법령명 추출 시도
                                for law_id, law in collected.items():
                                    if any(keyword in pdf_file.name for keyword in [law['law_name'], law_id]):
                                        # 별표/별첨에 OCR 텍스트 추가
                                        ocr_attachment = {
                                            'type': 'OCR 추출',
                                            'number': '',
                                            'title': pdf_file.name,
                                            'content': text
                                        }
                                        law['attachments'].append(ocr_attachment)
                                        st.info(f"'{law['law_name']}'에 OCR 텍스트 추가됨")
                                        break
                            else:
                                st.warning(f"❌ {pdf_file.name}: 텍스트 추출 실패")
                        except Exception as e:
                            st.error(f"OCR 처리 오류: {str(e)}")
    
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

    collected_by_file: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for file_key, laws in selected_by_file.items():
        for law in laws:
            law_id = law.get('law_id')
            if not law_id:
                continue
            detail = collected.get(law_id)
            if not detail:
                continue
            target = collected_by_file.setdefault(file_key, {})
            target[law_id] = detail

    if auto_added_ids and 'direct_input' in selected_by_file:
        direct_bucket = collected_by_file.setdefault('direct_input', {})
        for law_id in auto_added_ids:
            direct_bucket[law_id] = collected[law_id]

    st.session_state.collected_laws_by_file = collected_by_file

    # 통계 표시
    display_collection_stats(collected)
    display_hierarchy_overview(collected)


def extract_text_from_pdf(pdf_file) -> str:
    """PDF에서 텍스트 추출 (OCR)"""
    text = ""
    
    try:
        # pdfplumber로 텍스트 추출 시도
        with pdfplumber.open(pdf_file) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
        
        # 텍스트가 없으면 PyPDF2로 재시도
        if not text.strip():
            pdf_file.seek(0)
            pdf_reader = PyPDF2.PdfReader(pdf_file)
            for page in pdf_reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
    
    except Exception as e:
        logger.error(f"PDF 텍스트 추출 오류: {e}")
        
    return text.strip()


def display_collection_stats(collected_laws: Dict[str, Dict[str, Any]]):
    """수집 통계 표시 - 별표/별첨 텍스트 통계로 변경"""
    total_articles = sum(len(law.get('articles', [])) for law in collected_laws.values())
    total_provisions = sum(len(law.get('supplementary_provisions', [])) for law in collected_laws.values())
    total_attachments = sum(len(law.get('attachments', [])) for law in collected_laws.values())
    admin_rule_count = sum(1 for law in collected_laws.values() if law.get('is_admin_rule', False))
    
    # 별표/별첨 텍스트 길이 계산
    total_attachment_chars = sum(
        len(att.get('content', '')) 
        for law in collected_laws.values() 
        for att in law.get('attachments', [])
    )
    
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric("총 조문", f"{total_articles:,}개")
    with col2:
        st.metric("총 부칙", f"{total_provisions}개")
    with col3:
        st.metric("총 별표/별첨", f"{total_attachments}개")
    with col4:
        st.metric("행정규칙", f"{admin_rule_count}개")
    with col5:
        st.metric("별표/별첨 텍스트", f"{total_attachment_chars:,}자")


def display_hierarchy_overview(collected_laws: Dict[str, Dict[str, Any]]):
    """자동 확장된 법령 계층을 트리 형태로 표시"""
    if not collected_laws:
        return

    child_map: Dict[str, List[str]] = defaultdict(list)
    for law_id, law in collected_laws.items():
        parent_id = law.get('parent_law_id')
        if parent_id and parent_id in collected_laws:
            child_map[parent_id].append(law_id)

    if not child_map:
        return

    for children in child_map.values():
        children.sort(key=lambda cid: collected_laws[cid]['law_name'])

    root_ids = [law_id for law_id, law in collected_laws.items() if not law.get('parent_law_id')]
    root_ids.sort(key=lambda rid: collected_laws[rid]['law_name'])

    def render_node(node_id: str, level: int = 0) -> None:
        detail = collected_laws[node_id]
        relation = detail.get('relationship_from_parent')
        emoji = "📋" if detail.get('is_admin_rule') else "📖"
        indent = "&nbsp;" * (level * 4)
        label = f"{emoji} {detail['law_name']}"
        if relation:
            label += f" <span style='color:#888'>({relation})</span>"
        st.markdown(f"{indent}- {label}", unsafe_allow_html=True)

        for child_id in child_map.get(node_id, []):
            render_node(child_id, level + 1)

    with st.expander("🌳 자동으로 확장된 법령 체계도", expanded=True):
        for root_id in root_ids:
            render_node(root_id)


def display_download_section():
    """다운로드 섹션 표시 - 모든 형식 지원"""
    if not st.session_state.collected_laws:
        return

    st.header("💾 다운로드")

    exporter = LawExporter()

    # 체계도 검색 결과인 경우 특별 다운로드 옵션 표시
    is_hierarchy_search = st.session_state.get('current_data_type') == 'hierarchy'
    hierarchy_info = st.session_state.get('hierarchy_info')

    # 다운로드 옵션
    st.subheader("📥 다운로드 옵션")

    if is_hierarchy_search:
        download_option = st.radio(
            "다운로드 방식 선택",
            ["📊 통합 파일 (Merge)", "개별 파일 (ZIP)", "통합 파일 (단일)"],
            help="통합 파일 (Merge): 모든 법령을 체계도 형식으로 하나의 문서로 병합\n개별 파일: 각 법령별로 파일 생성\n통합 파일 (단일): 기존 단일 파일 형식"
        )
    else:
        download_option = st.radio(
            "다운로드 방식 선택",
            ["개별 파일 (ZIP)", "통합 파일 (단일)"],
            help="개별 파일: 각 법령별로 파일 생성\n통합 파일: 모든 법령을 하나의 파일로"
        )
    
    # Merge 다운로드 (체계도 검색 결과용)
    if download_option == "📊 통합 파일 (Merge)":
        st.info("📊 **법령 체계도 통합 다운로드**: 모든 법령을 하나의 문서로 병합합니다.")

        # 기준 법령명 추출
        base_law_name = ""
        if hierarchy_info:
            base_law_name = hierarchy_info.get('law_name', '')

        # 형식 선택
        merge_format = st.selectbox(
            "통합 파일 형식",
            ["Markdown (통합 + 개별 ZIP)", "Markdown 단일 파일", "JSON 단일 파일"],
            help="Markdown (통합 + 개별 ZIP): 통합 문서와 개별 파일을 모두 포함한 ZIP\nMarkdown 단일: 통합 Markdown 파일만\nJSON 단일: 전체 데이터를 JSON으로"
        )

        # 통계 표시
        total_laws = len(st.session_state.collected_laws)
        total_articles = sum(len(law.get('articles', [])) for law in st.session_state.collected_laws.values())
        total_attachments = sum(len(law.get('attachments', [])) for law in st.session_state.collected_laws.values())

        st.markdown(f"""
        **통합 파일 내용:**
        - 📚 법령 수: {total_laws}개
        - 📖 조문 수: {total_articles}개
        - 📎 별표/별첨: {total_attachments}개
        """)

        if merge_format == "Markdown (통합 + 개별 ZIP)":
            # 통합 + 개별 ZIP
            zip_data = exporter.export_merged_zip(st.session_state.collected_laws, base_law_name)

            st.download_button(
                label="📦 통합 ZIP 다운로드 (Merge + 개별)",
                data=zip_data,
                file_name=f"{base_law_name or '법령'}_체계도_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
                mime="application/zip",
                use_container_width=True
            )

        elif merge_format == "Markdown 단일 파일":
            # Markdown 단일 파일
            merged_md = exporter.export_merged_markdown(st.session_state.collected_laws, base_law_name)

            # 파일 크기 표시
            file_size = len(merged_md.encode('utf-8'))
            st.caption(f"📊 예상 파일 크기: {file_size:,} bytes ({file_size/1024:.1f} KB)")

            st.download_button(
                label="📄 통합 Markdown 다운로드",
                data=merged_md,
                file_name=f"{base_law_name or '법령'}_체계도_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md",
                mime="text/markdown",
                use_container_width=True
            )

            # 미리보기
            with st.expander("📄 내용 미리보기 (처음 2000자)"):
                st.markdown(merged_md[:2000] + "..." if len(merged_md) > 2000 else merged_md)

        else:  # JSON 단일 파일
            # JSON 데이터
            json_data = {
                'collection_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'base_law_name': base_law_name,
                'total_laws': total_laws,
                'hierarchy_info': hierarchy_info,
                'laws': st.session_state.collected_laws
            }
            json_content = json.dumps(json_data, ensure_ascii=False, indent=2)

            # 파일 크기 표시
            file_size = len(json_content.encode('utf-8'))
            st.caption(f"📊 예상 파일 크기: {file_size:,} bytes ({file_size/1024:.1f} KB)")

            st.download_button(
                label="📄 통합 JSON 다운로드",
                data=json_content,
                file_name=f"{base_law_name or '법령'}_체계도_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                mime="application/json",
                use_container_width=True
            )

    elif download_option == "개별 파일 (ZIP)":
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
        # 통합 파일 - 명확한 형식 선택
        st.info("📌 단일 파일로 모든 법령을 통합하여 다운로드합니다.")

        # 형식별 설명 추가
        format_descriptions = {
            "JSON": "구조화된 데이터 형식 (프로그래밍 활용에 적합)",
            "Markdown": "읽기 쉬운 문서 형식 (GitHub, 노션 등에 적합)",
            "Text": "순수 텍스트 형식 (메모장 등에서 열기 가능)"
        }
        
        file_format = st.selectbox(
            "파일 형식 선택",
            ["JSON", "Markdown", "Text"],
            help="다운로드할 파일 형식을 선택하세요"
        )
        
        st.caption(f"💡 {format_descriptions[file_format]}")
        
        # 형식별 내보내기 처리
        if file_format == "JSON":
            content = exporter.export_single_file(st.session_state.collected_laws, 'json')
            mime = "application/json"
            ext = "json"
        elif file_format == "Markdown":
            content = exporter.export_single_file(st.session_state.collected_laws, 'markdown')
            mime = "text/markdown"
            ext = "md"
        else:  # Text
            content = exporter.export_single_file(st.session_state.collected_laws, 'text')
            mime = "text/plain"
            ext = "txt"
        
        # 파일 크기 표시
        file_size = len(content.encode('utf-8'))
        st.caption(f"📊 예상 파일 크기: {file_size:,} bytes")
        
        st.download_button(
            label=f"💾 {file_format} 통합 파일 다운로드 (.{ext})",
            data=content,
            file_name=f"all_laws_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}",
            mime=mime,
            use_container_width=True
        )
        
        # 미리보기 옵션
        with st.expander("📄 내용 미리보기 (처음 1000자)"):
            st.text(content[:1000] + "..." if len(content) > 1000 else content)

    file_grouped = {
        key: laws
        for key, laws in st.session_state.get('collected_laws_by_file', {}).items()
        if laws
    }

    if file_grouped:
        st.subheader("🗂️ 파일별 Markdown 묶음")
        st.caption("업로드한 각 파일별로 통합된 Markdown 문서를 ZIP으로 제공합니다.")

        file_bundle = exporter.export_markdown_by_file(
            file_grouped,
            st.session_state.get('file_extractions', {})
        )

        st.download_button(
            label="🗂️ 파일별 Markdown ZIP 다운로드",
            data=file_bundle,
            file_name=f"file_grouped_markdown_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
            mime="application/zip",
            use_container_width=True
        )

    # 수집 결과 상세
    with st.expander("📊 수집 결과 상세"):
        for law_id, law in st.session_state.collected_laws.items():
            emoji = "📋" if law.get('is_admin_rule', False) else "📖"
            st.subheader(f"{emoji} {law['law_name']}")
            
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.write(f"조문: {len(law.get('articles', []))}개")
            with col2:
                st.write(f"부칙: {len(law.get('supplementary_provisions', []))}개")
            with col3:
                st.write(f"별표: {len(law.get('attachments', []))}개")
            with col4:
                # 별표/별첨 텍스트 길이
                att_chars = sum(len(att.get('content', '')) for att in law.get('attachments', []))
                if att_chars > 0:
                    st.write(f"별표 텍스트: {att_chars:,}자")
            
            # 샘플 조문
            if law.get('articles'):
                st.write("**샘플 조문:**")
                sample = law['articles'][0]
                st.text(f"{sample['number']} {sample.get('title', '')}")
                st.text(sample['content'][:200] + "...")
            
            # 별표/별첨 목록
            if law.get('attachments'):
                st.write("**별표/별첨:**")
                for att in law['attachments']:
                    st.write(f"  - {att['type']} {att.get('number', '')}: {att.get('title', '')} ({len(att.get('content', ''))}자)")


def main():
    """메인 함수"""
    # 세션 상태 초기화
    initialize_session_state()
    
    # 제목
    st.title("📚 법제처 법령 수집기")
    st.markdown("법제처 Open API를 활용한 법령 수집 도구 (v7.0)")
    st.markdown("**✨ 신규 기능: 자치법규, 판례, 헌재결정례, 법령해석례, 행정심판례, 조약 검색 지원**")
    
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
