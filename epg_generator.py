#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
EPG Generator - XMLTV
=====================

Gerador de EPG para IPTV utilizando múltiplas fontes:

1. TV Assembleia do Piauí
2. TVMap
3. mi.tv
4. Guia de TV

Características:

- XMLTV
- XMLTV.GZ
- múltiplas fontes
- fallback automático
- normalização de nomes
- aliases
- deduplicação
- fusos horários
- cache local
- retry automático
- logs
- GitHub Actions
- configuração por JSON
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import os
import re
import sys
import time

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo


# ============================================================
# CONFIGURAÇÕES
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

OUTPUT_DIR = BASE_DIR / "output"
CACHE_DIR = BASE_DIR / ".cache"

OUTPUT_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)

CONFIG_FILE = BASE_DIR / "config.json"

DEFAULT_TIMEZONE = "America/Fortaleza"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/139.0 Safari/537.36"
)

REQUEST_TIMEOUT = 30
MAX_RETRIES = 3

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("EPG")


# ============================================================
# MODELOS
# ============================================================

@dataclass
class Program:
    channel_id: str
    channel_name: str
    title: str

    start: datetime
    stop: datetime

    description: str = ""

    category: str = ""

    source: str = ""

    rating: str = ""


@dataclass
class Channel:
    channel_id: str
    name: str
    display_name: str
    logo: str = ""


# ============================================================
# UTILIDADES
# ============================================================

def normalize_text(text: str) -> str:
    if not text:
        return ""

    text = re.sub(r"\s+", " ", text)
    text = text.strip()

    return text


def normalize_channel_name(name: str) -> str:
    name = normalize_text(name)

    replacements = {
        "TV Cultura": "TV Cultura",
        "Tv Cultura": "TV Cultura",
        "tv cultura": "TV Cultura",

        "TV Brasil": "TV Brasil",
        "Tv Brasil": "TV Brasil",

        "SBT": "SBT",

        "Rede TV": "RedeTV!",
        "RedeTV": "RedeTV!",

        "TV Assembleia": "TV Assembleia",
        "TV Assembleia PI": "TV Assembleia",
        "Assembleia Legislativa do Piauí": "TV Assembleia",
    }

    if name in replacements:
        return replacements[name]

    return name


def slugify(text: str) -> str:
    text = normalize_text(text)

    text = text.lower()

    text = (
        text.replace("á", "a")
        .replace("à", "a")
        .replace("ã", "a")
        .replace("â", "a")
        .replace("ä", "a")
        .replace("é", "e")
        .replace("ê", "e")
        .replace("ë", "e")
        .replace("í", "i")
        .replace("ï", "i")
        .replace("ó", "o")
        .replace("õ", "o")
        .replace("ô", "o")
        .replace("ö", "o")
        .replace("ú", "u")
        .replace("ü", "u")
        .replace("ç", "c")
    )

    text = re.sub(r"[^a-z0-9]+", "-", text)

    return text.strip("-")


def make_channel_id(name: str) -> str:
    return f"{slugify(name)}.br"


def parse_time(time_text: str) -> Optional[Tuple[int, int]]:
    """
    Aceita:
    20:00
    20h00
    20.00
    """

    if not time_text:
        return None

    time_text = time_text.strip().lower()

    match = re.search(
        r"(\d{1,2})\s*[:h.]\s*(\d{2})",
        time_text,
    )

    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))

    if hour > 23 or minute > 59:
        return None

    return hour, minute


def parse_date(text: str) -> Optional[datetime]:
    formats = [
        "%d/%m/%Y",
        "%Y-%m-%d",
        "%d-%m-%Y",
    ]

    text = text.strip()

    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass

    return None


def xmltv_time(dt: datetime) -> str:
    """
    XMLTV utiliza:
    YYYYMMDDHHMMSS -0300
    """

    offset = dt.strftime("%z")

    return dt.strftime("%Y%m%d%H%M%S") + " " + offset


def xml_escape(text: str) -> str:
    if text is None:
        return ""

    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


# ============================================================
# HTTP
# ============================================================

class HTTPClient:

    def __init__(self):
        self.session = requests.Session()

        self.session.headers.update(HEADERS)

    def get(
        self,
        url: str,
        *,
        timeout: int = REQUEST_TIMEOUT,
    ) -> Optional[requests.Response]:

        for attempt in range(1, MAX_RETRIES + 1):

            try:

                logger.info(
                    "GET %s (tentativa %s/%s)",
                    url,
                    attempt,
                    MAX_RETRIES,
                )

                response = self.session.get(
                    url,
                    timeout=timeout,
                )

                response.raise_for_status()

                return response

            except Exception as exc:

                logger.warning(
                    "Erro HTTP: %s",
                    exc,
                )

                if attempt < MAX_RETRIES:
                    time.sleep(2 * attempt)

        return None


HTTP = HTTPClient()


# ============================================================
# CACHE
# ============================================================

def cache_key(url: str) -> str:
    return hashlib.sha256(
        url.encode("utf-8")
    ).hexdigest()


def get_cached(url: str) -> Optional[str]:

    path = CACHE_DIR / f"{cache_key(url)}.html"

    if not path.exists():
        return None

    try:

        age = time.time() - path.stat().st_mtime

        # Cache de 6 horas
        if age > 21600:
            return None

        return path.read_text(
            encoding="utf-8",
            errors="ignore",
        )

    except Exception:
        return None


def save_cache(url: str, content: str):

    path = CACHE_DIR / f"{cache_key(url)}.html"

    try:

        path.write_text(
            content,
            encoding="utf-8",
        )

    except Exception as exc:

        logger.warning(
            "Não foi possível salvar cache: %s",
            exc,
        )


def fetch_html(url: str) -> Optional[str]:

    cached = get_cached(url)

    if cached:
        logger.info("Usando cache: %s", url)
        return cached

    response = HTTP.get(url)

    if response is None:
        return None

    content = response.text

    save_cache(
        url,
        content,
    )

    return content


# ============================================================
# FONTE: GUIADETV
# ============================================================

class GuiaDeTVSource:

    name = "guiadetv"

    base_url = "https://www.guiadetv.com"

    def discover_channels(self) -> Dict[str, str]:

        channels = {}

        url = f"{self.base_url}/programacao"

        html = fetch_html(url)

        if not html:
            return channels

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        # Links individuais /canal/nome
        for link in soup.select("a[href*='/canal/']"):

            href = link.get("href", "")

            name = normalize_text(
                link.get_text(" ", strip=True)
            )

            if not name:
                continue

            if "/canal/" not in href:
                continue

            full_url = urljoin(
                self.base_url,
                href,
            )

            channels[name] = full_url

        logger.info(
            "GuiaDeTV: %s canais encontrados",
            len(channels),
        )

        return channels

    def parse_channel(
        self,
        channel_name: str,
        url: str,
        days: int,
    ) -> List[Program]:

        programs = []

        html = fetch_html(url)

        if not html:
            return programs

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        timezone = ZoneInfo(
            DEFAULT_TIMEZONE
        )

        today = datetime.now(
            timezone
        ).date()

        # Procura blocos de programação
        current_date = today

        # Estrutura encontrada no site:
        # horário
        # título
        #
        # Em páginas individuais há headings
        # para cada dia.

        elements = soup.find_all(
            ["h1", "h2", "h3", "h4", "time", "div"]
        )

        last_time = None
        last_title = None

        for element in elements:

            text = normalize_text(
                element.get_text(
                    " ",
                    strip=True,
                )
            )

            if not text:
                continue

            parsed = parse_time(text)

            if parsed:

                last_time = parsed

                continue

            if (
                last_time
                and len(text) >= 2
                and len(text) <= 200
            ):

                # Evita textos que claramente
                # não são programas.
                ignored = {
                    "Hoje",
                    "Amanhã",
                    "Programação da TV",
                    "Programação",
                }

                if text in ignored:
                    continue

                last_title = text

                hour, minute = last_time

                start = datetime.combine(
                    current_date,
                    datetime.min.time(),
                ).replace(
                    hour=hour,
                    minute=minute,
                    tzinfo=timezone,
                )

                # Próximo programa terá o horário
                # seguinte. Inicialmente usamos 1 hora.
                stop = start + timedelta(
                    hours=1
                )

                programs.append(
                    Program(
                        channel_id=make_channel_id(
                            normalize_channel_name(
                                channel_name
                            )
                        ),
                        channel_name=normalize_channel_name(
                            channel_name
                        ),
                        title=last_title,
                        start=start,
                        stop=stop,
                        source=self.name,
                    )
                )

                last_time = None
                last_title = None

        # Corrigir stop baseado no próximo programa
        programs.sort(
            key=lambda x: x.start
        )

        for i in range(
            len(programs) - 1
        ):

            if (
                programs[i + 1].channel_id
                == programs[i].channel_id
            ):

                programs[i].stop = (
                    programs[i + 1].start
                )

        return programs


# ============================================================
# FONTE: MI.TV
# ============================================================

class MiTVSource:

    name = "mitv"

    base_url = "https://mi.tv/br"

    def discover_channels(self) -> Dict[str, str]:

        channels = {}

        urls = [
            f"{self.base_url}/programacao",
            f"{self.base_url}/sitemap",
        ]

        for url in urls:

            html = fetch_html(url)

            if not html:
                continue

            soup = BeautifulSoup(
                html,
                "html.parser",
            )

            for link in soup.find_all(
                "a",
                href=True,
            ):

                href = link.get(
                    "href",
                    "",
                )

                name = normalize_text(
                    link.get_text(
                        " ",
                        strip=True,
                    )
                )

                if not name:
                    continue

                if "/canal/" in href:

                    channels[name] = urljoin(
                        self.base_url,
                        href,
                    )

        logger.info(
            "mi.tv: %s canais encontrados",
            len(channels),
        )

        return channels

    def parse_channel(
        self,
        channel_name: str,
        url: str,
        days: int,
    ) -> List[Program]:

        programs = []

        html = fetch_html(url)

        if not html:
            return programs

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        timezone = ZoneInfo(
            DEFAULT_TIMEZONE
        )

        current_date = datetime.now(
            timezone
        ).date()

        # O site pode alterar sua estrutura.
        # Procuramos horários e títulos
        # de forma tolerante.

        texts = [
            normalize_text(
                x.get_text(
                    " ",
                    strip=True,
                )
            )
            for x in soup.find_all(
                ["div", "span", "a", "h1", "h2", "h3"]
            )
        ]

        times = []

        for text in texts:

            parsed = parse_time(text)

            if parsed:

                times.append(
                    (text, parsed)
                )

        for i, (
            raw_time,
            parsed,
        ) in enumerate(times):

            hour, minute = parsed

            start = datetime.combine(
                current_date,
                datetime.min.time(),
            ).replace(
                hour=hour,
                minute=minute,
                tzinfo=timezone,
            )

            title = ""

            # Tenta encontrar conteúdo
            # próximo ao horário.
            index = texts.index(
                raw_time
            )

            for candidate in texts[
                index + 1:index + 10
            ]:

                if (
                    candidate
                    and not parse_time(candidate)
                    and len(candidate) <= 200
                ):
                    title = candidate
                    break

            if not title:
                continue

            if i + 1 < len(times):

                next_hour, next_minute = times[
                    i + 1
                ][1]

                stop = datetime.combine(
                    current_date,
                    datetime.min.time(),
                ).replace(
                    hour=next_hour,
                    minute=next_minute,
                    tzinfo=timezone,
                )

                if stop <= start:
                    stop += timedelta(days=1)

            else:

                stop = start + timedelta(
                    hours=1
                )

            programs.append(
                Program(
                    channel_id=make_channel_id(
                        normalize_channel_name(
                            channel_name
                        )
                    ),
                    channel_name=normalize_channel_name(
                        channel_name
                    ),
                    title=title,
                    start=start,
                    stop=stop,
                    source=self.name,
                )
            )

        return programs


# ============================================================
# FONTE: TVMAP
# ============================================================

class TVMapSource:

    name = "tvmap"

    base_url = "https://tvmap.com.br"

    def discover_channels(self) -> Dict[str, str]:

        channels = {}

        url = f"{self.base_url}/Programacao"

        html = fetch_html(url)

        if not html:
            return channels

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        for link in soup.find_all(
            "a",
            href=True,
        ):

            href = link.get(
                "href",
                "",
            )

            name = normalize_text(
                link.get_text(
                    " ",
                    strip=True,
                )
            )

            if not name:
                continue

            # TVMap possui URLs de canais.
            if (
                "tvmap.com.br" in href
                and len(name) < 100
            ):

                channels[name] = urljoin(
                    self.base_url,
                    href,
                )

        logger.info(
            "TVMap: %s canais encontrados",
            len(channels),
        )

        return channels

    def parse_channel(
        self,
        channel_name: str,
        url: str,
        days: int,
    ) -> List[Program]:

        programs = []

        html = fetch_html(url)

        if not html:
            return programs

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        timezone = ZoneInfo(
            DEFAULT_TIMEZONE
        )

        current_date = datetime.now(
            timezone
        ).date()

        # Parser tolerante para alterações
        # de HTML.

        blocks = soup.find_all(
            ["div", "li", "article"]
        )

        for block in blocks:

            text = normalize_text(
                block.get_text(
                    " ",
                    strip=True,
                )
            )

            if not text:
                continue

            match = re.search(
                r"(\d{1,2}:\d{2})\s+(.+)",
                text,
            )

            if not match:
                continue

            time_text = match.group(1)
            title = normalize_text(
                match.group(2)
            )

            parsed = parse_time(
                time_text
            )

            if not parsed:
                continue

            hour, minute = parsed

            start = datetime.combine(
                current_date,
                datetime.min.time(),
            ).replace(
                hour=hour,
                minute=minute,
                tzinfo=timezone,
            )

            programs.append(
                Program(
                    channel_id=make_channel_id(
                        normalize_channel_name(
                            channel_name
                        )
                    ),
                    channel_name=normalize_channel_name(
                        channel_name
                    ),
                    title=title,
                    start=start,
                    stop=start + timedelta(
                        hours=1
                    ),
                    source=self.name,
                )
            )

        programs.sort(
            key=lambda x: x.start
        )

        for i in range(
            len(programs) - 1
        ):

            if (
                programs[i].start
                < programs[i + 1].start
            ):
                programs[i].stop = (
                    programs[i + 1].start
                )

        return programs


# ============================================================
# TV ASSEMBLEIA PIAUÍ
# ============================================================

class TVAssembleiaSource:

    name = "tv_assembleia"

    url = (
        "https://www.al.pi.leg.br/"
        "comunicacao/tv-assembleia/programacao"
    )

    def parse(
        self,
        days: int,
    ) -> List[Program]:

        programs = []

        html = fetch_html(
            self.url
        )

        if not html:
            logger.warning(
                "TV Assembleia: "
                "não foi possível acessar a página."
            )

            return programs

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        timezone = ZoneInfo(
            DEFAULT_TIMEZONE
        )

        current_date = datetime.now(
            timezone
        ).date()

        elements = soup.find_all(
            ["div", "p", "li", "td", "span"]
        )

        for element in elements:

            text = normalize_text(
                element.get_text(
                    " ",
                    strip=True,
                )
            )

            if not text:
                continue

            match = re.search(
                r"(\d{1,2}[:h.]\d{2})\s*[-–—]\s*(.+)",
                text,
            )

            if not match:
                continue

            time_text = match.group(1)

            title = normalize_text(
                match.group(2)
            )

            parsed = parse_time(
                time_text
            )

            if not parsed:
                continue

            hour, minute = parsed

            start = datetime.combine(
                current_date,
                datetime.min.time(),
            ).replace(
                hour=hour,
                minute=minute,
                tzinfo=timezone,
            )

            programs.append(
                Program(
                    channel_id="tv-assembleia-pi.br",
                    channel_name="TV Assembleia",
                    title=title,
                    start=start,
                    stop=start + timedelta(
                        hours=1
                    ),
                    source=self.name,
                )
            )

        programs.sort(
            key=lambda x: x.start
        )

        for i in range(
            len(programs) - 1
        ):

            programs[i].stop = (
                programs[i + 1].start
            )

        return programs


# ============================================================
# CONFIG
# ============================================================

def load_config() -> dict:

    if not CONFIG_FILE.exists():

        return {
            "days": 2,
            "timezone": DEFAULT_TIMEZONE,
            "channels": [],
            "aliases": {},
        }

    try:

        return json.loads(
            CONFIG_FILE.read_text(
                encoding="utf-8"
            )
        )

    except Exception as exc:

        logger.warning(
            "Erro lendo config.json: %s",
            exc,
        )

        return {
            "days": 2,
            "timezone": DEFAULT_TIMEZONE,
            "channels": [],
            "aliases": {},
        }


# ============================================================
# DEDUPLICAÇÃO
# ============================================================

def deduplicate_programs(
    programs: List[Program],
) -> List[Program]:

    result = []

    seen = set()

    for program in programs:

        key = (
            program.channel_id,
            program.start.isoformat(),
            program.title.lower(),
        )

        if key in seen:
            continue

        seen.add(key)

        result.append(
            program
        )

    result.sort(
        key=lambda x: (
            x.channel_id,
            x.start,
        )
    )

    return result


# ============================================================
# MESCLAGEM DE FONTES
# ============================================================

def merge_sources(
    source_programs: Dict[str, List[Program]],
) -> List[Program]:

    """
    Prioridade:

    TV Assembleia
    TVMap
    mi.tv
    GuiaDeTV
    """

    priority = [
        "tv_assembleia",
        "tvmap",
        "mitv",
        "guiadetv",
    ]

    merged = []

    by_channel = {}

    for source_name in priority:

        for program in source_programs.get(
            source_name,
            [],
        ):

            key = program.channel_id

            if key not in by_channel:
                by_channel[key] = []

            by_channel[key].append(
                program
            )

    for channel_id, programs in by_channel.items():

        # Mantém primeiro as fontes
        # de maior prioridade.

        selected = []

        used_slots = set()

        for source_name in priority:

            source_items = [
                p
                for p in programs
                if p.source == source_name
            ]

            for program in source_items:

                slot = (
                    program.start,
                    program.stop,
                )

                if slot in used_slots:
                    continue

                selected.append(
                    program
                )

                used_slots.add(
                    slot
                )

        merged.extend(
            selected
        )

    return deduplicate_programs(
        merged
    )


# ============================================================
# XMLTV
# ============================================================

def build_channels(
    programs: List[Program],
) -> List[Channel]:

    channels = {}

    for program in programs:

        if program.channel_id not in channels:

            channels[
                program.channel_id
            ] = Channel(
                channel_id=program.channel_id,
                name=program.channel_name,
                display_name=program.channel_name,
            )

    return list(
        channels.values()
    )


def generate_xmltv(
    programs: List[Program],
) -> str:

    channels = build_channels(
        programs
    )

    lines = []

    lines.append(
        '<?xml version="1.0" encoding="UTF-8"?>'
    )

    lines.append(
        '<tv generator-info-name="IPTV EPG Generator" '
        'generator-info-url="https://github.com/">'
    )

    # --------------------------------------------------------
    # CANAIS
    # --------------------------------------------------------

    for channel in channels:

        lines.append(
            f'  <channel id="{xml_escape(channel.channel_id)}">'
        )

        lines.append(
            f'    <display-name lang="pt">{xml_escape(channel.display_name)}</display-name>'
        )

        lines.append(
            "  </channel>"
        )

    # --------------------------------------------------------
    # PROGRAMAS
    # --------------------------------------------------------

    for program in programs:

        lines.append(
            f'  <programme '
            f'channel="{xml_escape(program.channel_id)}" '
            f'start="{xmltv_time(program.start)}" '
            f'stop="{xmltv_time(program.stop)}">'
        )

        lines.append(
            f'    <title lang="pt">{xml_escape(program.title)}</title>'
        )

        if program.description:

            lines.append(
                f'    <desc lang="pt">{xml_escape(program.description)}</desc>'
            )

        if program.category:

            lines.append(
                f'    <category lang="pt">{xml_escape(program.category)}</category>'
            )

        lines.append(
            "  </programme>"
        )

    lines.append(
        "</tv>"
    )

    return "\n".join(
        lines
    ) + "\n"


# ============================================================
# ALIASES
# ============================================================

def generate_aliases(
    programs: List[Program],
    config: dict,
):

    aliases = {}

    for program in programs:

        aliases[
            program.channel_name
        ] = program.channel_id

    # aliases definidos pelo usuário
    aliases.update(
        config.get(
            "aliases",
            {}
        )
    )

    output = (
        OUTPUT_DIR /
        "epg_aliases.json"
    )

    output.write_text(
        json.dumps(
            aliases,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ============================================================
# STATUS
# ============================================================

def generate_status(
    programs: List[Program],
    source_programs: Dict[str, List[Program]],
):

    channels = set(
        p.channel_id
        for p in programs
    )

    status = {
        "generated_at": datetime.now(
            ZoneInfo(DEFAULT_TIMEZONE)
        ).isoformat(),

        "channels": len(channels),

        "programs": len(programs),

        "sources": {
            source: len(items)
            for source, items
            in source_programs.items()
        },
    }

    output = (
        OUTPUT_DIR /
        "epg_status.json"
    )

    output.write_text(
        json.dumps(
            status,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ============================================================
# COMPRESSÃO
# ============================================================

def gzip_file(
    source: Path,
    destination: Path,
):

    with source.open(
        "rb"
    ) as input_file:

        with gzip.open(
            destination,
            "wb",
            compresslevel=9,
        ) as output_file:

            while True:

                chunk = input_file.read(
                    1024 * 1024
                )

                if not chunk:
                    break

                output_file.write(
                    chunk
                )


# ============================================================
# EXECUÇÃO PRINCIPAL
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description="Gerador EPG XMLTV multisource"
    )

    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Quantidade de dias",
    )

    parser.add_argument(
        "--output",
        default=str(OUTPUT_DIR),
        help="Diretório de saída",
    )

    args = parser.parse_args()

    config = load_config()

    days = (
        args.days
        if args.days is not None
        else config.get(
            "days",
            2,
        )
    )

    logger.info(
        "=========================================="
    )

    logger.info(
        "        IPTV EPG GENERATOR"
    )

    logger.info(
        "=========================================="
    )

    logger.info(
        "Dias: %s",
        days,
    )

    # --------------------------------------------------------
    # FONTES
    # --------------------------------------------------------

    source_programs = {
        "tv_assembleia": [],
        "tvmap": [],
        "mitv": [],
        "guiadetv": [],
    }

    # --------------------------------------------------------
    # TV ASSEMBLEIA
    # --------------------------------------------------------

    try:

        source = TVAssembleiaSource()

        programs = source.parse(
            days
        )

        source_programs[
            source.name
        ] = programs

        logger.info(
            "TV Assembleia: %s programas",
            len(programs),
        )

    except Exception as exc:

        logger.exception(
            "Erro TV Assembleia: %s",
            exc,
        )

    # --------------------------------------------------------
    # GUIADETV
    # --------------------------------------------------------

    try:

        source = GuiaDeTVSource()

        channels = source.discover_channels()

        for channel_name, url in channels.items():

            try:

                programs = source.parse_channel(
                    channel_name,
                    url,
                    days,
                )

                source_programs[
                    source.name
                ].extend(
                    programs
                )

            except Exception as exc:

                logger.warning(
                    "GuiaDeTV %s: %s",
                    channel_name,
                    exc,
                )

        logger.info(
            "GuiaDeTV total: %s programas",
            len(
                source_programs[
                    source.name
                ]
            ),
        )

    except Exception as exc:

        logger.exception(
            "Erro GuiaDeTV: %s",
            exc,
        )

    # --------------------------------------------------------
    # MI.TV
    # --------------------------------------------------------

    try:

        source = MiTVSource()

        channels = source.discover_channels()

        for channel_name, url in channels.items():

            try:

                programs = source.parse_channel(
                    channel_name,
                    url,
                    days,
                )

                source_programs[
                    source.name
                ].extend(
                    programs
                )

            except Exception as exc:

                logger.warning(
                    "mi.tv %s: %s",
                    channel_name,
                    exc,
                )

        logger.info(
            "mi.tv total: %s programas",
            len(
                source_programs[
                    source.name
                ]
            ),
        )

    except Exception as exc:

        logger.exception(
            "Erro mi.tv: %s",
            exc,
        )

    # --------------------------------------------------------
    # TVMAP
    # --------------------------------------------------------

    try:

        source = TVMapSource()

        channels = source.discover_channels()

        for channel_name, url in channels.items():

            try:

                programs = source.parse_channel(
                    channel_name,
                    url,
                    days,
                )

                source_programs[
                    source.name
                ].extend(
                    programs
                )

            except Exception as exc:

                logger.warning(
                    "TVMap %s: %s",
                    channel_name,
                    exc,
                )

        logger.info(
            "TVMap total: %s programas",
            len(
                source_programs[
                    source.name
                ]
            ),
        )

    except Exception as exc:

        logger.exception(
            "Erro TVMap: %s",
            exc,
        )

    # --------------------------------------------------------
    # MERGE
    # --------------------------------------------------------

    programs = merge_sources(
        source_programs
    )

    logger.info(
        "TOTAL FINAL: %s programas",
        len(programs),
    )

    if not programs:

        logger.error(
            "Nenhum programa encontrado."
        )

        sys.exit(1)

    # --------------------------------------------------------
    # XML
    # --------------------------------------------------------

    xml = generate_xmltv(
        programs
    )

    output_xml = (
        OUTPUT_DIR /
        "epg.xml"
    )

    output_xml.write_text(
        xml,
        encoding="utf-8",
    )

    logger.info(
        "Gerado: %s",
        output_xml,
    )

    # --------------------------------------------------------
    # GZIP
    # --------------------------------------------------------

    output_gz = (
        OUTPUT_DIR /
        "epg.xml.gz"
    )

    gzip_file(
        output_xml,
        output_gz,
    )

    logger.info(
        "Gerado: %s",
        output_gz,
    )

    # --------------------------------------------------------
    # ALIASES
    # --------------------------------------------------------

    generate_aliases(
        programs,
        config,
    )

    # --------------------------------------------------------
    # STATUS
    # --------------------------------------------------------

    generate_status(
        programs,
        source_programs,
    )

    logger.info(
        "=========================================="
    )

    logger.info(
        "EPG FINALIZADO COM SUCESSO"
    )

    logger.info(
        "=========================================="
    )


if __name__ == "__main__":
    main()