"""
법제처 법령 수집기 - PDF 다운로드 개선 및 OCR 지원 버전 (v6.9)
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
from typing import List, Set, Dict, Optional, Tuple, Any
from dataclasses import dataclass
from functools import lru_cache
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import base64
import urllib.parse

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
                        model="gpt-3.5-turbo",
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
                    # GPT-3.5가 실패하면 GPT-4 시도
                    try:
                        response = client.chat.completions.create(
                            model="gpt-4",
                            messages=[
                                {"role": "system", "content": "한국 법령 전문가. 법령체계도에서 법령명을 정확히 추출하고, 특수문자 변환과 사용자 의도를 파악합니다."},
                                {"role": "user", "content": prompt}
                            ],
                            temperature=0.1,
                            max_tokens=1500
                        )
                        
                        ai_laws = self._parse_ai_response_enhanced(response.choices[0].message.content)
                        self.logger.info(f"GPT-4로 {len(ai_laws - laws)}개의 법령을 추가로 찾았습니다.")
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
        if is_admin_rule:
            return self._get_admin_rule_detail(law_msn, law_name)
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
    
    def _get_admin_rule_detail(self, law_msn: str, law_name: str) -> Optional[Dict[str, Any]]:
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
            return self._parse_admin_rule_detail(response.text, law_msn, law_name)
            
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
            'is_admin_rule': False
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
            
            # 원문 저장 (조문이 없는 경우)
            if not detail['articles']:
                detail['raw_content'] = self._extract_full_text(root)
                
            self.logger.info(f"상세 정보 파싱 완료: {law_name} - 조문 {len(detail['articles'])}개, 별표/별첨 {len(detail['attachments'])}개")
                
        except Exception as e:
            self.logger.error(f"상세 정보 파싱 오류: {e}")
            
        return detail
    
    def _parse_admin_rule_detail(self, content: str, law_msn: str, 
                                law_name: str) -> Dict[str, Any]:
        """행정규칙 상세 정보 파싱"""
        detail = {
            'law_id': '',
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
            'is_admin_rule': True
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
                
            self.logger.info(f"행정규칙 상세 파싱 완료: {law_name} - 조문 {len(detail['articles'])}개, 별표/별첨 {len(detail['attachments'])}개")
                
        except Exception as e:
            self.logger.error(f"행정규칙 상세 파싱 오류: {e}")
            
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
        'oc_code': '',
        'include_pdfs': False  # PDF 다운로드 옵션
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
                                                model="gpt-3.5-turbo",
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
                                                # 구버전 API 호환
                                                try:
                                                    # 간단한 완료 테스트
                                                    test_response = test_client.completions.create(
                                                        model="text-davinci-003",
                                                        prompt="test",
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


def handle_direct_search_mode(oc_code: str):
    """직접 검색 모드 처리"""
    st.header("🔍 직접 검색 모드")
    
    # 직접 검색 모드 설명
    with st.info("💡 직접 검색 모드에서는 띄어쓰기 변형을 포함하여 최대한 많은 법령을 찾습니다."):
        st.caption("예: '공인노무사법시행령' → '공인노무사법 시행령'도 함께 검색")
    
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
                
                # 직접 검색은 변형 검색을 사용
                results = collector._search_with_variations(law_name)
                
                if results:
                    st.success(f"{len(results)}개의 법령을 찾았습니다!")
                    
                    # 행정규칙 개수 표시
                    admin_count = sum(1 for r in results if r.get('is_admin_rule'))
                    if admin_count > 0:
                        st.info(f"📋 이 중 {admin_count}개는 행정규칙입니다.")
                    
                    # 어떤 변형으로 찾았는지 표시
                    variations_used = set()
                    for r in results:
                        if 'found_with_variation' in r:
                            variations_used.add(r['found_with_variation'])
                    
                    if variations_used and len(variations_used) > 1:
                        with st.expander("🔍 검색에 사용된 변형"):
                            for var in variations_used:
                                st.write(f"- {var}")
                    
                    st.session_state.search_results = results
                else:
                    st.warning("검색 결과가 없습니다.")
                    st.info("💡 Tip: 띄어쓰기를 조정하거나 기관코드를 확인해보세요.")
                    st.session_state.search_results = []


def handle_file_upload_mode(oc_code: str):
    """파일 업로드 모드 처리"""
    st.header("📄 파일 업로드 모드")
    
    # AI 상태 표시 (수정)
    if st.session_state.use_ai and st.session_state.openai_api_key:
        st.info(f"🤖 AI 강화 모드 활성화")
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
            # API 키 전달 확인
            logger.info(f"AI 사용 여부: {st.session_state.use_ai}")
            logger.info(f"API 키 존재: {bool(st.session_state.openai_api_key)}")
            
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
                logger.error(f"파일 처리 오류: {e}", exc_info=True)
    
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
            # 파일 업로드 모드로 검색 (변형 검색 사용하지 않음)
            search_laws_from_list(oc_code, edited_laws or st.session_state.extracted_laws, is_from_file=True)


def search_laws_from_list(oc_code: str, law_names: List[str], is_from_file: bool = True):
    """법령 목록 검색
    
    Args:
        oc_code: 기관코드
        law_names: 검색할 법령명 리스트
        is_from_file: 파일에서 추출된 법령인지 여부 (기본값: True)
                     - True: 법령체계도 모드 (정확한 검색)
                     - False: 직접 입력 모드 (변형 검색)
    """
    collector = LawCollectorAPI(oc_code)
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    def update_progress(progress):
        progress_bar.progress(progress)
    
    # 모드 표시
    if is_from_file:
        st.info("📋 법령체계도 모드: 추출된 법령명과 정확히 일치하는 법령만 검색합니다.")
    else:
        st.info("🔍 직접 검색 모드: 띄어쓰기 변형 등을 포함하여 포괄적으로 검색합니다.")
    
    with st.spinner("법령을 검색하는 중..."):
        # use_variations 파라미터를 모드에 따라 설정
        results = collector.search_laws(
            law_names, 
            progress_callback=update_progress,
            use_variations=(not is_from_file)  # 파일 모드일 때는 False
        )
    
    progress_bar.progress(1.0)
    status_text.text("검색 완료!")
    
    if results:
        st.success(f"✅ 총 {len(results)}개의 법령을 찾았습니다!")
        
        # 행정규칙 통계
        admin_count = sum(1 for r in results if r.get('is_admin_rule'))
        if admin_count > 0:
            st.info(f"📋 이 중 {admin_count}개는 행정규칙입니다.")
        
        # 법령체계도 모드에서는 추가 정보 표시
        if is_from_file:
            with st.expander("💡 검색 모드 정보"):
                st.write("**법령체계도 모드**에서는 다음과 같이 작동합니다:")
                st.write("- ✅ 추출된 법령명과 정확히 일치하는 법령만 검색")
                st.write("- ❌ 띄어쓰기 변형이나 유사 법령명 검색하지 않음")
                st.write("- 💡 법령체계도에 명시된 법령만 수집하여 정확성 보장")
        
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
            if law.get('is_admin_rule'):
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
                           if law.get('is_admin_rule'))
        if selected_admin > 0:
            st.info(f"📋 선택된 행정규칙: {selected_admin}개")
        
        # 수집 버튼
        if st.button("📥 선택한 법령 수집", type="primary", use_container_width=True):
            collect_selected_laws(oc_code)


def collect_selected_laws(oc_code: str):
    """선택된 법령 수집 - PDF 다운로드 제거, 텍스트 내용 활용"""
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
    
    # 통계 표시
    display_collection_stats(collected)


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


def display_download_section():
    """다운로드 섹션 표시 - 모든 형식 지원"""
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
    st.markdown("법제처 Open API를 활용한 법령 수집 도구 (v6.9)")
    st.markdown("**✨ 개선사항: PDF 다운로드 → OCR 텍스트 추출, 초기화 시 설정 유지**")
    
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
