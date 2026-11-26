"""Panel do ETS2 i ATS na telefon/tablet: serwer w sieci domowej, który pokazuje telemetrię i zamienia stuknięcia na klawisze w grze.

Uruchomienie: python serwer.py  (albo panel.exe). Na komputerze otworzy się strona startowa z kodem QR,
na telefonie wystarczy go zeskanować. Działa po Wi-Fi w tej samej sieci albo po USB (adb reverse).
Klawisze idą przez SendInput z kodami skanowania i TYLKO wtedy, gdy na pierwszym planie jest gra.
Tylko biblioteka standardowa Pythona.
"""
import ctypes
import ipaddress
import json
import math
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import webbrowser
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WERSJA = "0.1.0"
PORT = int(os.environ.get("PANEL_PORT", "8765"))
# w paczce .exe (PyInstaller) pliki leżą w katalogu tymczasowym _MEIPASS
KATALOG = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
WWW = KATALOG / "www"
WTYCZKA = KATALOG / "wtyczka" / "scs-telemetry.dll"
CZAS_STUKNIECIA = 0.06  # gry czytają klawiaturę co klatkę, za krótkie wciśnięcie potrafią przegapić
GRY = {"eurotrucks2.exe": "ets2", "amtrucks.exe": "ats"}
NAZWY_GIER = {"ets2": "Euro Truck Simulator 2", "ats": "American Truck Simulator"}

# Kody skanowania (zestaw 1). Rozszerzone (z prefiksem E0) mają flagę True.
KLAWISZE = {
    **{c: (k, False) for c, k in zip("1234567890", range(0x02, 0x0C))},
    "q": (0x10, False), "w": (0x11, False), "e": (0x12, False), "r": (0x13, False), "t": (0x14, False),
    "y": (0x15, False), "u": (0x16, False), "i": (0x17, False), "o": (0x18, False), "p": (0x19, False),
    "minus": (0x0C, False), "lbracket": (0x1A, False), "rbracket": (0x1B, False), "backslash": (0x2B, False),
    "a": (0x1E, False), "s": (0x1F, False), "d": (0x20, False), "f": (0x21, False), "g": (0x22, False),
    "h": (0x23, False), "j": (0x24, False), "k": (0x25, False), "l": (0x26, False),
    "semicolon": (0x27, False), "apostrophe": (0x28, False),
    "z": (0x2C, False), "x": (0x2D, False), "c": (0x2E, False), "v": (0x2F, False), "b": (0x30, False),
    "n": (0x31, False), "m": (0x32, False), "comma": (0x33, False), "period": (0x34, False), "slash": (0x35, False),
    "space": (0x39, False), "enter": (0x1C, False), "esc": (0x01, False), "tab": (0x0F, False), "backspace": (0x0E, False),
    "shift": (0x2A, False), "ctrl": (0x1D, False), "alt": (0x38, False),
    **{f"f{n}": (0x3A + n, False) for n in range(1, 11)}, "f11": (0x57, False), "f12": (0x58, False),
    "insert": (0x52, True), "delete": (0x53, True), "home": (0x47, True), "end": (0x4F, True),
    "pageup": (0x49, True), "pagedown": (0x51, True),
    "up": (0x48, True), "down": (0x50, True), "left": (0x4B, True), "right": (0x4D, True),
}

# --- SendInput -------------------------------------------------------------
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x1, 0x2, 0x8
ULONG_PTR = ctypes.c_size_t


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):  # tylko po to, żeby unia miała właściwy rozmiar
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)


def wyslij(nazwy, wcisniety):
    """Wciska albo puszcza klawisze; puszczanie w odwrotnej kolejności (najpierw litera, potem ctrl)."""
    kolejne = nazwy if wcisniety else list(reversed(nazwy))
    zdarzenia = (INPUT * len(kolejne))()
    for i, nazwa in enumerate(kolejne):
        kod, rozszerzony = KLAWISZE[nazwa]
        flagi = KEYEVENTF_SCANCODE | (0 if wcisniety else KEYEVENTF_KEYUP) | (KEYEVENTF_EXTENDEDKEY if rozszerzony else 0)
        zdarzenia[i].type = 1  # INPUT_KEYBOARD
        zdarzenia[i].ki = KEYBDINPUT(0, kod, flagi, 0, 0)
    user32.SendInput(len(kolejne), zdarzenia, ctypes.sizeof(INPUT))


# --- Profile ---------------------------------------------------------------
def wczytaj_akcje():
    """Mapa 'profil/id' -> lista klawiszy. Tablet wysyła tylko id, więc nie da się nim wpisać dowolnego tekstu."""
    profile = json.loads((WWW / "profile.json").read_text(encoding="utf-8"))
    akcje = {}
    for nazwa_profilu, profil in profile.items():
        if nazwa_profilu.startswith("_"):
            continue
        for sekcja in profil["sekcje"]:
            for p in sekcja.get("przyciski", []):  # sekcja z joystickiem nie ma przycisków
                nieznane = [k for k in p["klawisz"] + p.get("klawisz_wyl", []) if k not in KLAWISZE]
                if nieznane:
                    raise SystemExit(f"profile.json: {nazwa_profilu}/{p['id']} ma nieznany klawisz {nieznane}")
                akcje[f"{nazwa_profilu}/{p['id']}"] = p["klawisz"]
                if "klawisz_wyl" in p:  # wyłączenie innym klawiszem niż włączenie (np. lusterko -> widok na wprost)
                    akcje[f"{nazwa_profilu}/{p['id']}/wyl"] = p["klawisz_wyl"]
    return akcje


wcisniete = {}  # akcja -> klawisze trzymane (typ "trzymaj")
blokada = threading.Lock()


def wykonaj(akcje, akcja, tryb):
    klawisze = akcje[akcja]
    with blokada:
        if tryb == "stuknij":
            wyslij(klawisze, True)
            time.sleep(CZAS_STUKNIECIA)
            wyslij(klawisze, False)
        elif tryb == "wcisnij" and akcja not in wcisniete:
            wyslij(klawisze, True)
            wcisniete[akcja] = klawisze
        elif tryb == "pusc" and akcja in wcisniete:
            wyslij(wcisniete.pop(akcja), False)


def pusc_wszystko():
    with blokada:
        for klawisze in wcisniete.values():
            wyslij(klawisze, False)
        wcisniete.clear()


# --- Rozglądanie (joystick na tablecie -> ruch myszy) ----------------------
# Tablet podaje, gdzie ma patrzeć głowa (piksele myszy od widoku na wprost). Pętla co 10 ms
# dochodzi tam ruchem myszy i pamięta, ile przesunęła, więc po puszczeniu kciuka cofa dokładnie tyle samo.
MOUSEEVENTF_MOVE = 0x1
LIMIT_PATRZENIA = 3000   # px, na wypadek bzdur z sieci
KROK_PATRZENIA = 80      # px na takt: płynnie, ale bez szarpnięć
BEZ_SYGNALU_S = 1.0      # tablet zgasł albo zerwało połączenie -> wracamy na wprost
patrzenie = {"cel": [0, 0], "pozycja": [0, 0], "ostatnio": 0.0}
patrz_blokada = threading.Lock()
petla_patrzenia_dziala = False


def ruch_myszy(dx, dy):
    zdarzenie = INPUT()
    zdarzenie.type = 0  # INPUT_MOUSE
    zdarzenie.mi = MOUSEINPUT(dx, dy, 0, MOUSEEVENTF_MOVE, 0, 0)
    user32.SendInput(1, ctypes.byref(zdarzenie), ctypes.sizeof(INPUT))


def ustaw_patrzenie(x, y):
    with patrz_blokada:
        patrzenie["cel"] = [max(-LIMIT_PATRZENIA, min(LIMIT_PATRZENIA, int(x))),
                            max(-LIMIT_PATRZENIA, min(LIMIT_PATRZENIA, int(y)))]
        patrzenie["ostatnio"] = time.monotonic()


def petla_patrzenia():
    while True:
        time.sleep(0.01)
        with patrz_blokada:
            if patrzenie["cel"] != [0, 0] and time.monotonic() - patrzenie["ostatnio"] > BEZ_SYGNALU_S:
                patrzenie["cel"] = [0, 0]
            kroki = []
            for i in (0, 1):
                roznica = patrzenie["cel"][i] - patrzenie["pozycja"][i]
                krok = max(-KROK_PATRZENIA, min(KROK_PATRZENIA, round(roznica * 0.35))) or (roznica > 0) - (roznica < 0)
                patrzenie["pozycja"][i] += krok
                kroki.append(krok)
        if kroki != [0, 0]:
            ruch_myszy(*kroki)


# --- Telemetria ETS2 -------------------------------------------------------
# Wtyczka scs-sdk-plugin (RenCloud, v1.12.1) w ETS2\bin\win_x64\plugins wystawia stan ciężarówki
# w pamięci współdzielonej. Dzięki temu panel pokazuje to, co naprawdę jest w grze,
# np. kierunkowskaz zgaszony przez grę po skręcie. Przesunięcia ze scs-telemetry-common.hpp (strefa bool od 1500).
MMF_NAZWA = "Local\\SCSTelemetry"
MMF_ROZMIAR = 32 * 1024
POLA_ETS2 = {
    "reczny": 1566, "hamulec_silnikowy": 1567, "zaplon": 1575, "silnik": 1576, "lewy": 1578, "prawy": 1579,  # lewy/prawy = włączony, nie "akurat świeci"
    "postojowe": 1582, "mijania": 1583, "drogowe": 1584, "kogut": 1585, "cofanie": 1587, "awaryjne": 1588, "tempomat": 1589,
}
# liczby do klastra (float, strefa od 700): m/s, metry, sekundy, litry, psi, °C, V, km, 0-1
LICZBY_ETS2 = {
    "paliwo_max": 704, "adblue_max": 712, "obroty_max": 740, "masa_kg": 748,
    "predkosc": 948, "obroty": 952, "tempomat_kmh": 988, "powietrze_psi": 992,
    "paliwo": 1000, "spalanie_lkm": 1004, "zasieg": 1008, "adblue": 1012,
    "olej_psi": 1016, "olej_c": 1020, "woda_c": 1024, "akumulator_v": 1028,
    "zuz_silnik": 1036, "zuz_skrzynia": 1040, "zuz_kabina": 1044, "zuz_podwozie": 1048, "zuz_kola": 1052,
    "przebieg": 1056, "dystans": 1060, "czas": 1064, "limit": 1068,
}
# ostrzeżenia z deski (bool) i reszta: liczby całkowite, napisy po 64 B, zarobek (u64)
OSTRZEZENIA_ETS2 = {"powietrze": 1568, "powietrze_alarm": 1569, "paliwo": 1570, "adblue": 1571,
                    "olej": 1572, "woda": 1573, "akumulator": 1574}
CZAS_GRY, TERMIN, TRASA_KM, BIEG = 64, 88, 100, 508   # minuty od początku gry / km / bieg z deski
NAPISY_ETS2 = {"ladunek": 2620, "cel_miasto": 2748, "cel_firma": 2876, "start_miasto": 3004}
ZAROBEK, NA_ZLECENIU = 4000, 4300
PSI_NA_BAR = 0.0689476
POZYCJA_X, POZYCJA_Z = 2200, 2216  # double, metry w świecie gry (truck_dp.coordinateX / Z)

# --- Gdzie jestem: współrzędne gry -> stopnie geograficzne -> najbliższe miejsce z mapy ETS2 ---
# Rzutowanie z def/climate.sii (stożkowe Lamberta), jak w projekcie truckermudgeon/maps (libs/map/projections.ts).
# miejsca_ets2.json = [dł, szer, kod kraju, nazwa] miast, firm, krajobrazów (z truckermudgeon.github.io/ets2-search.geojson).
KRAJE = {"AL": "Albania", "AT": "Austria", "BA": "Bośnia i Hercegowina", "BE": "Belgia", "BG": "Bułgaria",
         "CH": "Szwajcaria", "CZ": "Czechy", "DE": "Niemcy", "DK": "Dania", "EE": "Estonia", "ES": "Hiszpania",
         "FI": "Finlandia", "FR": "Francja", "GB": "Wielka Brytania", "GR": "Grecja", "HR": "Chorwacja",
         "HU": "Węgry", "IS": "Islandia", "IT": "Włochy", "LT": "Litwa", "LU": "Luksemburg", "LV": "Łotwa",
         "ME": "Czarnogóra", "MK": "Macedonia Płn.", "NL": "Holandia", "NO": "Norwegia", "PL": "Polska",
         "PT": "Portugalia", "RO": "Rumunia", "RS": "Serbia", "RU": "Rosja", "SE": "Szwecja", "SI": "Słowenia",
         "SK": "Słowacja", "TR": "Turcja", "XK": "Kosowo"}
# ATS: stany USA (miejsca_ats.json z truckermudgeon.github.io/ats-search.geojson, ten sam format)
STANY = {"AR": "Arkansas", "AZ": "Arizona", "CA": "Kalifornia", "CO": "Kolorado", "IA": "Iowa", "ID": "Idaho",
         "IL": "Illinois", "KS": "Kansas", "LA": "Luizjana", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
         "NM": "Nowy Meksyk", "NV": "Nevada", "OK": "Oklahoma", "OR": "Oregon", "SD": "Dakota Płd.", "TX": "Teksas",
         "UT": "Utah", "WA": "Waszyngton", "WY": "Wyoming", "ND": "Dakota Płn.", "MN": "Minnesota", "WI": "Wisconsin"}
_R = 6_370_997
_STOPIEN = _R * math.pi / 180


def _lcc(fi1, fi2, fi0, lambda0):
    """Stałe stożkowego Lamberta (sfera) z def/climate.sii: n, F, rho0, południk środkowy."""
    fi1, fi2, fi0 = (math.radians(v) for v in (fi1, fi2, fi0))
    n = math.log(math.cos(fi1) / math.cos(fi2)) / math.log(math.tan(math.pi / 4 + fi2 / 2) / math.tan(math.pi / 4 + fi1 / 2))
    f = math.cos(fi1) * math.tan(math.pi / 4 + fi1 / 2) ** n / n
    return n, f, _R * f / math.tan(math.pi / 4 + fi0 / 2) ** n, math.radians(lambda0)


_LCC = {"ets2": _lcc(37, 65, 50, 15), "ats": _lcc(33, 45, 39, -96)}
_miejsca = {}


def gra_na_geo(x, y, gra="ets2"):
    """(x, z) z gry -> (długość, szerokość) w stopniach."""
    if gra == "ats":
        lx, ly = x * 0.000176689948 * _STOPIEN, y * -0.00017706234 * _STOPIEN
    else:
        sx, sy = math.floor(x / 4000), math.floor(y / 4000)
        x, y = x - 16660, y - 4150
        if sx <= -8 and sy <= -2 and not (sx == -8 and sy == -2):  # Wyspy Brytyjskie mają w grze inną skalę
            x, y = (x - 31100 / 2) * 0.75, (y - 5500 / 2) * 0.75
        lx, ly = x * 0.0001729241463 * _STOPIEN, y * -0.000171570875 * _STOPIEN
    n, f, rho0, lambda0 = _LCC[gra]
    rho = math.copysign(math.hypot(lx, rho0 - ly), n)
    theta = math.atan2(lx, rho0 - ly)
    szer = 2 * math.atan((_R * f / rho) ** (1 / n)) - math.pi / 2
    return math.degrees(lambda0 + theta / n), math.degrees(szer)


def gdzie_jestem(x, y, gra="ets2"):
    gra = "ats" if gra == "ats" else "ets2"
    if gra not in _miejsca:
        _miejsca[gra] = json.loads((WWW / f"miejsca_{gra}.json").read_text(encoding="utf-8"))
    dl, sz = gra_na_geo(x, y, gra)
    kos = math.cos(math.radians(sz))
    odl2, m = min((((p[0] - dl) * kos) ** 2 + (p[1] - sz) ** 2, p) for p in _miejsca[gra])
    nazwy = STANY if gra == "ats" else KRAJE
    return {"kraj_kod": m[2], "kraj": nazwy.get(m[2], m[2]), "w_poblizu": m[3],
            "odl_km": round(math.sqrt(odl2) * 111.2), "geo": [round(dl, 4), round(sz, 4)]}
FILE_MAP_READ = 0x4

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenFileMappingW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
kernel32.OpenFileMappingW.restype = wintypes.HANDLE
kernel32.MapViewOfFile.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t)
kernel32.MapViewOfFile.restype = ctypes.c_void_p
widok_ets2 = None  # adres zmapowanej pamięci; uchwyt trzymamy do końca, restart gry go nie psuje


def telemetria_ets2(gra="ets2"):
    """Stany przycisków z gry albo None, gdy wtyczka nie działa (gra wyłączona, wtyczki brak).
    Ta sama wtyczka i ta sama pamięć działa w ATS; gra decyduje tylko o mapie (Europa / USA)."""
    global widok_ets2
    if widok_ets2 is None:
        uchwyt = kernel32.OpenFileMappingW(FILE_MAP_READ, False, MMF_NAZWA)
        if not uchwyt:
            return None
        widok_ets2 = kernel32.MapViewOfFile(uchwyt, FILE_MAP_READ, 0, 0, MMF_ROZMIAR)
        if not widok_ets2:
            widok_ets2 = None
            return None
    dane = ctypes.string_at(widok_ets2, 4400)
    if not dane[0]:  # sdkActive
        return None
    stany = {nazwa: dane[przes] for nazwa, przes in POLA_ETS2.items()}
    # przycisk świateł w panelu ma kolejne stany WYŁ / POSTÓJ / MIJANIA
    mijania, postojowe = stany.pop("mijania"), stany.pop("postojowe")
    stany["swiatla"] = 2 if mijania else 1 if postojowe else 0
    stany["info"] = info_ets2(dane, gra)
    return stany


def info_ets2(dane, gra="ets2"):
    """Dane do klastra na dole panelu (deska rozdzielcza + GPS), już w jednostkach do pokazania."""
    f = {nazwa: struct.unpack_from("<f", dane, przes)[0] for nazwa, przes in LICZBY_ETS2.items()}
    u = lambda o: struct.unpack_from("<I", dane, o)[0]
    zuzycie = max(f["zuz_silnik"], f["zuz_skrzynia"], f["zuz_kabina"], f["zuz_podwozie"], f["zuz_kola"])
    return {
        "kmh": round(abs(f["predkosc"]) * 3.6),
        "limit": round(f["limit"] * 3.6),
        "tempomat_kmh": round(f["tempomat_kmh"] * 3.6),
        "bieg": struct.unpack_from("<i", dane, BIEG)[0],
        "obroty": round(f["obroty"]), "obroty_max": round(f["obroty_max"]),
        "paliwo_l": round(f["paliwo"]), "paliwo_max": round(f["paliwo_max"]),
        "spalanie_l100": round(f["spalanie_lkm"] * 100, 1),
        "zasieg_km": round(f["zasieg"]),
        "adblue_l": round(f["adblue"]), "adblue_max": round(f["adblue_max"]),
        "powietrze_bar": round(f["powietrze_psi"] * PSI_NA_BAR, 2),
        "olej_bar": round(f["olej_psi"] * PSI_NA_BAR, 1),
        "olej_c": round(f["olej_c"]), "woda_c": round(f["woda_c"]),
        "akumulator_v": round(f["akumulator_v"], 1),
        "zuzycie_proc": round(zuzycie * 100),
        "przebieg_km": round(f["przebieg"], 1),
        "do_celu_km": round(f["dystans"] / 1000, 1),
        "do_celu_min": round(f["czas"] / 60),
        "czas_gry": u(CZAS_GRY), "termin": u(TERMIN), "trasa_km": u(TRASA_KM),
        "na_zleceniu": bool(dane[NA_ZLECENIU]),
        "masa_t": round(f["masa_kg"] / 1000, 1),
        "zarobek": struct.unpack_from("<Q", dane, ZAROBEK)[0],
        **{nazwa: _napis(dane, przes, 64) for nazwa, przes in NAPISY_ETS2.items()},
        "ostrzezenia": [nazwa for nazwa, przes in OSTRZEZENIA_ETS2.items() if dane[przes]],
        "miejsce": _miejsce(dane, gra),
    }


def _miejsce(dane, gra="ets2"):
    x, z = struct.unpack_from("<d", dane, POZYCJA_X)[0], struct.unpack_from("<d", dane, POZYCJA_Z)[0]
    if x == 0 and z == 0:
        return None
    try:
        return gdzie_jestem(x, z, gra)
    except (OSError, ValueError, ZeroDivisionError):
        return None


def _napis(dane, przes, dlugosc):
    return dane[przes:przes + dlugosc].split(b"\0")[0].decode("utf-8", "replace")


# --- Która gra działa i czy jest na pierwszym planie -------------------------
TH32CS_SNAPPROCESS = 0x2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ULONG_PTR), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260)]


def dzialajaca_gra():
    """'ets2' / 'ats' / None. Lista procesów przez Toolhelp, bez odpalania tasklist."""
    migawka = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not migawka or migawka == ctypes.c_void_p(-1).value:
        return None
    try:
        wpis = PROCESSENTRY32W()
        wpis.dwSize = ctypes.sizeof(wpis)
        ok = kernel32.Process32FirstW(migawka, ctypes.byref(wpis))
        while ok:
            gra = GRY.get(wpis.szExeFile.lower())
            if gra:
                return gra
            ok = kernel32.Process32NextW(migawka, ctypes.byref(wpis))
    finally:
        kernel32.CloseHandle(migawka)
    return None


def gra_na_pierwszym_planie():
    """Klawisze wysyłamy tylko do gry. Bez tego stuknięcie w telefon wpisałoby literę np. w okno czatu."""
    okno = user32.GetForegroundWindow()
    if not okno:
        return False
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(okno, ctypes.byref(pid))
    proces = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not proces:
        return False
    try:
        bufor = ctypes.create_unicode_buffer(1024)
        rozmiar = wintypes.DWORD(len(bufor))
        if not kernel32.QueryFullProcessImageNameW(proces, 0, bufor, ctypes.byref(rozmiar)):
            return False
        return Path(bufor.value).name.lower() in GRY
    finally:
        kernel32.CloseHandle(proces)


kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))


# --- Gdzie są gry (Steam) i wtyczka telemetrii --------------------------------
def katalogi_steam():
    """Biblioteki Steama z rejestru + libraryfolders.vdf."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = Path(winreg.QueryValueEx(k, "SteamPath")[0])
    except OSError:
        return []
    biblioteki = [steam]
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    try:
        for linia in vdf.read_text(encoding="utf-8", errors="replace").splitlines():
            czesci = linia.strip().split('"')
            if len(czesci) >= 4 and czesci[1] == "path":
                biblioteki.append(Path(czesci[3].replace("\\\\", "\\")))
    except OSError:
        pass
    return list(dict.fromkeys(biblioteki))


def stan_gier():
    """Dla każdej gry: czy jest zainstalowana i czy ma wtyczkę telemetrii."""
    wynik = {}
    for gra, nazwa in NAZWY_GIER.items():
        katalog = next((b / "steamapps" / "common" / nazwa for b in katalogi_steam()
                        if (b / "steamapps" / "common" / nazwa / "bin" / "win_x64").is_dir()), None)
        wtyczki = katalog / "bin" / "win_x64" / "plugins" if katalog else None
        wynik[gra] = {"nazwa": nazwa, "katalog": str(katalog) if katalog else None,
                      "wtyczka": bool(wtyczki and (wtyczki / "scs-telemetry.dll").exists())}
    return wynik


def zainstaluj_wtyczke(gra):
    stan = stan_gier().get(gra)
    if not stan or not stan["katalog"]:
        return "nie znaleziono gry w bibliotekach Steama"
    if dzialajaca_gra() == gra:
        return "zamknij grę i spróbuj jeszcze raz"
    cel = Path(stan["katalog"]) / "bin" / "win_x64" / "plugins"
    try:
        cel.mkdir(exist_ok=True)
        shutil.copy2(WTYCZKA, cel / "scs-telemetry.dll")
    except OSError as e:
        return f"nie udało się skopiować: {e}"
    return None


# --- Sieć i zapora ------------------------------------------------------------
def dozwolony_adres(adres):
    """Tylko sieć domowa i ten komputer. Z internetu panel nie odpowiada."""
    ip = ipaddress.ip_address(adres)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local


def adres_lan():
    """Adres komputera w sieci domowej. connect() na UDP nic nie wysyła, tylko wybiera trasę."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # adres testowy z RFC 5737, nigdzie nie trafia
            ip = s.getsockname()[0]
            if ipaddress.ip_address(ip).is_private:
                return ip
    except OSError:
        pass
    return "127.0.0.1"


NAZWA_REGULY = "Panel ETS2 ATS"


def jest_regula_zapory():
    wynik = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={NAZWA_REGULY}"],
                           capture_output=True, creationflags=0x08000000)
    return wynik.returncode == 0


def dodaj_regule_zapory():
    """Jedno okienko UAC. Reguła wpuszcza port panelu tylko z sieci lokalnej (LocalSubnet), w każdym profilu,
    bo Windows często uznaje domową sieć za publiczną i wtedy telefon nie może się połączyć."""
    argumenty = (f'advfirewall firewall add rule name="{NAZWA_REGULY}" dir=in action=allow protocol=TCP '
                 f'localport={PORT} remoteip=LocalSubnet profile=any')
    kod = ctypes.windll.shell32.ShellExecuteW(None, "runas", "netsh", argumenty, None, 0)
    return kod > 32  # >32 = uruchomione (użytkownik mógł jeszcze odmówić w UAC)


# --- USB (opcjonalnie): adb reverse --------------------------------------------
def znajdz_adb():
    for kandydat in (KATALOG / "adb" / "adb.exe", Path(sys.executable).parent / "adb" / "adb.exe"):
        if kandydat.exists():
            return str(kandydat)
    return shutil.which("adb")


def petla_usb():
    """Co 5 s: jeśli telefon jest podpięty z debugowaniem USB, robi tunel. Wtedy na telefonie działa
    http://localhost:PORT bez Wi-Fi i bez zapory. Bez adb po prostu nic nie robi."""
    adb = znajdz_adb()
    if not adb:
        return
    znane = set()
    while True:
        try:
            wynik = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=10, creationflags=0x08000000)
            urzadzenia = {l.split("\t")[0] for l in wynik.stdout.splitlines()[1:] if l.endswith("\tdevice")}
            for nowe in urzadzenia - znane:
                subprocess.run([adb, "-s", nowe, "reverse", f"tcp:{PORT}", f"tcp:{PORT}"],
                               capture_output=True, timeout=10, creationflags=0x08000000)
            znane = urzadzenia
        except (OSError, subprocess.SubprocessError):
            pass
        time.sleep(5)


# --- HTTP ---------------------------------------------------------------------
TYPY = {".html": "text/html; charset=utf-8", ".json": "application/json; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".woff2": "font/woff2"}
TRASY = {"/": "panel.html", "/ets2": "panel.html", "/ats": "panel.html", "/start": "start.html",
         "/manifest.json": "manifest.json"}


class Obsluga(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # połączenie zostaje otwarte: przez Wi-Fi bez nowego TCP przy każdym stuknięciu

    def _dozwolony(self):
        if dozwolony_adres(self.client_address[0]):
            return True
        self.close_connection = True
        self.send_error(403)
        return False

    def _z_tego_komputera(self):
        return ipaddress.ip_address(self.client_address[0]).is_loopback and not self.headers.get("X-Forwarded-For")

    def _odpowiedz(self, kod, tresc=b"", typ="text/plain; charset=utf-8"):
        self.send_response(kod)
        self.send_header("Content-Type", typ)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(tresc)))
        self.end_headers()
        self.wfile.write(tresc)

    def _json(self, obiekt, kod=200):
        self._odpowiedz(kod, json.dumps(obiekt, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _plik(self, sciezka):
        nazwa = TRASY.get(sciezka) or sciezka.lstrip("/")
        plik = (WWW / nazwa).resolve()
        if WWW.resolve() not in plik.parents or not plik.is_file():  # bez wychodzenia poza www/
            return self.send_error(404)
        self._odpowiedz(200, plik.read_bytes(), TYPY.get(plik.suffix, "application/octet-stream"))

    def do_GET(self):
        if not self._dozwolony():
            return
        sciezka = self.path.split("?")[0]
        if sciezka == "/gra":  # panel sam przełącza się na grę, która właśnie działa
            return self._json({"gra": dzialajaca_gra()})
        if sciezka == "/telemetria":
            return self._json({"ets2": telemetria_ets2(dzialajaca_gra() or "ets2")})
        if sciezka == "/info":  # dla strony startowej
            return self._json({"wersja": WERSJA, "port": PORT, "adres": f"http://{adres_lan()}:{PORT}",
                               "gra": dzialajaca_gra(), "gry": stan_gier(), "zapora": jest_regula_zapory(),
                               "adb": bool(znajdz_adb())})
        self._plik(sciezka)

    def do_POST(self):
        if not self._dozwolony():
            return
        dlugosc = int(self.headers.get("Content-Length", 0) or 0)
        try:
            dane = json.loads(self.rfile.read(dlugosc) or b"{}")
        except ValueError:
            return self._odpowiedz(400, b"zle dane")
        if self.path in ("/wtyczka", "/zapora"):  # zmiany na komputerze tylko ze strony otwartej na nim samym
            if not self._z_tego_komputera():
                return self._odpowiedz(403, b"tylko z tego komputera")
            if self.path == "/zapora":
                return self._json({"ok": dodaj_regule_zapory()})
            blad = zainstaluj_wtyczke(dane.get("gra"))
            return self._json({"ok": blad is None, "blad": blad})
        if self.path == "/patrz":
            try:
                ustaw_patrzenie(dane["x"], dane["y"]) if gra_na_pierwszym_planie() else None
            except (KeyError, TypeError, ValueError):
                return self._odpowiedz(400, b"zle dane")
            return self._odpowiedz(204)
        if self.path != "/akcja":
            return self.send_error(404)
        try:
            if dane.get("tryb") == "pusc_wszystko":
                pusc_wszystko()
                return self._odpowiedz(204)
            self.server.akcje = wczytaj_akcje()  # zmiany w profile.json działają bez restartu
            if dane["akcja"] not in self.server.akcje or dane["tryb"] not in ("stuknij", "wcisnij", "pusc"):
                return self._odpowiedz(400, b"zla akcja")
            if dane["tryb"] != "pusc" and not gra_na_pierwszym_planie():
                return self._json({"blad": "gra nie jest na pierwszym planie"}, 409)
            wykonaj(self.server.akcje, dane["akcja"], dane["tryb"])
        except (KeyError, TypeError):
            return self._odpowiedz(400, b"zle dane")
        self._odpowiedz(204)

    def log_message(self, fmt, *args):
        pass


class Serwer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        """Telefon albo przeglądarka zerwały połączenie (zamknięta karta, uśpiony ekran): to normalne, nie straszymy."""
        if isinstance(sys.exc_info()[1], (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def main():
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\PanelETS2ATS")
    if ctypes.windll.kernel32.GetLastError() == 183:  # już działa: tylko pokaż stronę startową
        webbrowser.open(f"http://localhost:{PORT}/start")
        return
    serwer = Serwer(("0.0.0.0", PORT), Obsluga)
    serwer.akcje = wczytaj_akcje()
    threading.Thread(target=petla_patrzenia, daemon=True).start()
    threading.Thread(target=petla_usb, daemon=True).start()
    print(f"Panel ETS2/ATS {WERSJA}\n  telefon w tej samej sieci: http://{adres_lan()}:{PORT}\n"
          f"  telefon po USB (adb):      http://localhost:{PORT}\n  Zamknij to okno, żeby wyłączyć panel.")
    if "--bez-strony" not in sys.argv:
        webbrowser.open(f"http://localhost:{PORT}/start")
    try:
        serwer.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        pusc_wszystko()


if __name__ == "__main__":
    main()
