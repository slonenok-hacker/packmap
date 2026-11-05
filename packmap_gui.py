# -*- coding: utf-8 -*-
"""
packmap_gui.py — сборка ресурсов GoldSrc-карты (CS 1.6) в папку для раздачи.

GUI:           python packmap_gui.py
Командная строка:
    python packmap_gui.py map.bsp --game "C:/Games/Half-Life/cstrike" [--valve ".../valve"] [--extract-wad]

Что собирает:
  * WAD-текстуры   — из ключа "wad" у worldspawn; если все текстуры карты
                     вшиты в BSP (как у официальных карт), wad в пакет не кладутся;
  * скайбокс       — из ключа "skyname" (6 граней в gfx/env);
  * модели/спрайты — .mdl/.spr из всех значений энтити;
  * звуки          — .wav из всех значений энтити (добавляется sound/);
  * .res-файл      — список ресурсов для докачки клиентом;
  * [опционально]  — извлечение вшитых текстур в WAD3 (для редактирования в Hammer).

Результат: папка <мод>/maps/<имя карты>/ с готовой структурой:
архивируй её и раздавай (карта тоже копируется внутрь).
"""

import os
import re
import struct
import shutil
import sys

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    HAS_TK = True
except ImportError:
    HAS_TK = False

LUMP_COUNT = 15   # у GoldSrc 15 lumps, энтити — lump 0
MIPTEX_LUMP = 2   # miptex lump

QUOTED_RE = re.compile(r'"([^"]*)"')


# ------------------------------------------------------------------- BSP
def read_bsp_entities(bsp_path):
    """(версия, текст энтити). Порядок полей lump: fileofs, затем filelen."""
    with open(bsp_path, 'rb') as f:
        data = f.read()
    if len(data) < 4 + LUMP_COUNT * 8:
        raise ValueError('файл слишком мал для BSP-заголовка')
    version = struct.unpack_from('<i', data, 0)[0]
    lumps = [struct.unpack_from('<ii', data, 4 + i * 8) for i in range(LUMP_COUNT)]
    text = ''
    ofs, ln = lumps[0]
    if 0 <= ofs < len(data) and 0 < ln <= len(data) - ofs:
        text = data[ofs:ofs + ln].decode('cp1251', errors='replace')
    if 'classname' not in text:
        # аварийный поиск: если lump 0 не дал текст, ищем энтити по всем lumps
        for ofs2, ln2 in lumps:
            if 0 <= ofs2 < len(data) and 0 < ln2 <= len(data) - ofs2:
                chunk = data[ofs2:ofs2 + ln2]
                if b'classname' in chunk:
                    text = chunk.decode('cp1251', errors='replace')
                    break
    return version, text


def parse_dependencies(ent_text):
    """(wad_paths, skyname, [пути к mdl/spr/wav])."""
    wads = []
    m = re.search(r'"wad"\s+"([^"]*)"', ent_text, re.I)
    if m:
        for part in m.group(1).split(';'):
            p = part.strip().replace('\\', '/')
            if p:
                wads.append(p)
    sky = None
    m = re.search(r'"skyname"\s+"([^"]*)"', ent_text, re.I)
    if m:
        sky = m.group(1).strip()
    files, seen = [], set()
    for val in QUOTED_RE.findall(ent_text):
        v = val.strip().replace('\\', '/').lstrip('/')
        if not v or v.startswith('!'):
            continue
        low = v.lower()
        if low.endswith(('.mdl', '.spr')):
            rel = v
        elif low.endswith('.wav'):
            rel = v if low.startswith('sound/') else 'sound/' + v
        else:
            continue
        if rel.lower() not in seen:
            seen.add(rel.lower())
            files.append(rel)
    return wads, sky, files


# ----------------------------------------------------------------- miptex
def _mip_sizes(w, h):
    s0 = w * h
    return s0, s0 >> 2, s0 >> 4, s0 >> 6


def read_miptex_entries(bsp_data):
    """
    Читает miptex-кусок BSP (lump 2). Возвращает список записей:
      name, w, h, embedded, data_abs (абс. смещение mip-данных), mips_len.
    Заголовок записи BSP: name[16] + width(i32) + height(i32) + offsets[4](i32) = 40 байт.
    Смещения offsets[] бывают entry-относительные (обычно) или lump-относительные
    (старые компиляторы) — детектим автоматически.
    Внешняя текстура (ищется в wad): все offsets нулевые.
    """
    if len(bsp_data) < 4 + LUMP_COUNT * 8:
        return []
    lump_ofs, lump_len = struct.unpack_from('<ii', bsp_data, 4 + MIPTEX_LUMP * 8)
    if lump_ofs < 0 or lump_len <= 0 or lump_ofs + lump_len > len(bsp_data):
        return []
    lump = bsp_data[lump_ofs:lump_ofs + lump_len]
    if len(lump) < 4:
        return []
    n = struct.unpack_from('<i', lump, 0)[0]
    if n <= 0 or 4 + n * 4 > len(lump):
        return []
    dataofs = [struct.unpack_from('<i', lump, 4 + i * 4)[0] for i in range(n)]
    entries = []
    for ofs in dataofs:
        if ofs < 0 or ofs + 40 > len(lump):
            continue
        raw = lump[ofs:ofs + 40]
        name = raw[:16].split(b'\x00', 1)[0].decode('ascii', errors='replace')
        w, h, m0, m1, m2, m3 = struct.unpack_from('<6i', raw, 16)
        offs = (m0, m1, m2, m3)
        if all(o == 0 for o in offs):
            entries.append({'name': name, 'w': w, 'h': h, 'embedded': False,
                            'data_abs': 0, 'mips_len': 0})
            continue
        s0, s1, s2, s3 = _mip_sizes(w, h)
        tail = s3 + 768  # mip3 + палитра 256*3
        entry_abs = lump_ofs + ofs
        bound = min(len(bsp_data), lump_ofs + lump_len)
        if m0 == 40:              # entry-относительные смещения (стандарт)
            base = entry_abs
        elif m0 == ofs + 40:      # lump-относительные смещения
            base = lump_ofs
        else:
            base = entry_abs
        end_rel = m3 + tail
        if base + end_rel > bound:
            # пробуем другой базис
            alt = lump_ofs if base == entry_abs else entry_abs
            if alt + end_rel <= bound:
                base = alt
            else:
                continue  # битая запись
        entries.append({'name': name, 'w': w, 'h': h, 'embedded': True,
                        'data_abs': base + m0, 'mips_len': end_rel - m0})
    return entries


def analyze_textures_in_bsp(bsp_path):
    """(всего, вшито, внешних, entries)."""
    with open(bsp_path, 'rb') as f:
        bsp_data = f.read()
    entries = read_miptex_entries(bsp_data)
    emb = sum(1 for e in entries if e['embedded'])
    return len(entries), emb, len(entries) - emb, entries


# ------------------------------------------------------------- WAD export
def export_wad_from_bsp(bsp_path, wad_path):
    """
    Извлекает вшитые текстуры из BSP и сохраняет их в WAD3.
    Возвращает число экспортированных текстур.
    """
    with open(bsp_path, 'rb') as f:
        bsp_data = f.read()
    entries = read_miptex_entries(bsp_data)
    embedded = [e for e in entries if e['embedded']]
    if not embedded:
        return 0

    bodies = []
    for e in embedded:
        w, h = e['w'], e['h']
        s0, s1, s2, s3 = _mip_sizes(w, h)
        mip = bsp_data[e['data_abs']:e['data_abs'] + e['mips_len']]
        if len(mip) < s0 + s1 + s2 + s3 + 768:
            continue
        name_b = e['name'].encode('ascii', errors='replace')[:16].ljust(16, b'\x00')
        offs = (40, 40 + s0, 40 + s0 + s1, 40 + s0 + s1 + s2)
        header = name_b + struct.pack('<6i', w, h, *offs)
        bodies.append(name_b + header[16:] + mip)
    if not bodies:
        return 0

    # WAD3: заголовок 12 байт (WAD3 + numlumps + dirofs), затем данные, затем каталог
    out = bytearray(b'WAD3')
    offset = 12
    dir_rows = []
    for body in bodies:
        dir_rows.append((offset, len(body), body[:16]))
        offset += len(body)
    dirofs = offset
    out += struct.pack('<ii', len(bodies), dirofs)
    for _off, _sz, _n, body in [(r[0], r[1], r[2], b) for r, b in zip(dir_rows, bodies)]:
        out += body
    for off, sz, nb in dir_rows:
        # тип 0x43 = miptex, сжатие нет
        out += struct.pack('<iiiBBH16s', off, sz, sz, 0x43, 0, 0, nb.upper())
    with open(wad_path, 'wb') as f:
        f.write(bytes(out))
    return len(bodies)


def read_wad_names(wad_path):
    """Имена текстур из каталога WAD2/WAD3."""
    with open(wad_path, 'rb') as f:
        data = f.read()
    if len(data) < 12 or data[:4] not in (b'WAD3', b'WAD2'):
        return set()
    n, dirofs = struct.unpack_from('<ii', data, 4)
    names = set()
    if dirofs < 0 or dirofs + n * 32 > len(data):
        return names
    for i in range(n):
        raw = data[dirofs + i * 32:dirofs + i * 32 + 32]
        nm = raw[16:32].split(b'\x00', 1)[0].decode('ascii', errors='replace').upper()
        if nm:
            names.add(nm)
    return names


# ----------------------------------------------------------------- поиск
def build_index(dirs):
    index = {}
    for d in dirs:
        if d and os.path.isdir(d):
            for root, _sub, files in os.walk(d):
                for fn in files:
                    index.setdefault(fn.lower(), []).append(os.path.join(root, fn))
    return index


def find_file(rel_path, dirs, index):
    rel = rel_path.replace('\\', '/').lstrip('/')
    parts = rel.split('/')
    for d in dirs:
        cand = os.path.join(d, *parts)
        if os.path.isfile(cand):
            return cand
    base = os.path.basename(rel)
    cands = index.get(base.lower(), [])
    for cand in cands:
        if parts[0].lower() in cand.replace('\\', '/').lower().split('/'):
            return cand
    return cands[0] if cands else None


def copy_into(result_dir, rel_path, src):
    dest = os.path.join(result_dir, *rel_path.replace('\\', '/').split('/'))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy2(src, dest)
    return rel_path.replace('\\', '/')


# -------------------------------------------------------------- сборка
def collect_map(bsp_path, mod_dir, valve_dir=None, result_dir=None,
                extract_wad=False, log=print):
    bsp_path = os.path.abspath(bsp_path)
    if not os.path.isfile(bsp_path):
        raise FileNotFoundError('карта не найдена: ' + bsp_path)
    mod_dir = os.path.abspath(mod_dir)
    dirs = [mod_dir]
    if valve_dir and os.path.isdir(valve_dir):
        dirs.append(os.path.abspath(valve_dir))
    mapname = os.path.splitext(os.path.basename(bsp_path))[0]
    if not result_dir:
        result_dir = os.path.join(mod_dir, 'maps', mapname)
    result_dir = os.path.abspath(result_dir)
    os.makedirs(result_dir, exist_ok=True)

    log('=' * 64)
    log(f'Карта: {bsp_path}')
    log('Папки поиска: ' + '; '.join(dirs))
    log(f'Папка результата: {result_dir}')

    version, ent = read_bsp_entities(bsp_path)
    log(f'BSP версия: {version}; энтити: {len(ent)} симв.')
    if not ent or 'classname' not in ent:
        log('ВНИМАНИЕ: энтити не распознаны — пришли вывод автору утилиты.')
        return
    preview = ' '.join(ent[:300].split())
    log('Начало энтити: ' + preview + ('...' if len(ent) > 300 else ''))

    total_tex, emb_tex, ext_tex, entries = analyze_textures_in_bsp(bsp_path)
    log(f'Текстуры в BSP: вшито {emb_tex}, внешних {ext_tex} (всего {total_tex})')
    external_names = [e['name'].upper() for e in entries if not e['embedded']]

    wads, sky, files = parse_dependencies(ent)
    index = build_index(dirs)
    copied, missing = [], []

    log('-' * 64)
    if ext_tex == 0:
        log('Все текстуры вшиты в BSP — WAD для игры не нужны:')
        for w in wads:
            log(f'  [-] {os.path.basename(w)} — вшито, в пакет не кладём')
    else:
        log('WAD-текстуры: ' + (', '.join(os.path.basename(w) for w in wads) or 'нет'))
        covered = set()
        for w in wads:
            src = find_file(w, dirs, index)
            if not src:
                missing.append(w)
                log(f'  [??] НЕ НАЙДЕНО: {w}')
                continue
            names = read_wad_names(src)
            hit = [t for t in external_names if t in names]
            if hit:
                covered.update(hit)
                rel = copy_into(result_dir, os.path.basename(w), src)
                copied.append(rel)
                log(f'  [OK] {os.path.basename(w)} -> {rel} '
                    f'(покрывает {len(hit)} внешних текстур)')
            else:
                log(f'  [-] {os.path.basename(w)} — не покрывает внешних текстур, не нужен')
        uncovered = [t for t in external_names if t not in covered]
        if uncovered:
            log('  [!] Внешние текстуры без покрытия найденными WAD (у игроков будут фиолетовые):')
            for t in uncovered:
                log(f'      {t}')

    log(f'Скайбокс: {sky or "нет"}')
    if sky:
        ok_faces = 0
        for face in ('bk', 'ft', 'lf', 'rt', 'up', 'dn'):
            rel = f'gfx/env/{sky}{face}.tga'
            src = find_file(rel, dirs, index)
            if not src:
                src = find_file(f'gfx/env/{sky}{face}.bmp', dirs, index)
            if src:
                copy_into(result_dir, rel, src)
                copied.append(rel)
                ok_faces += 1
            else:
                missing.append(rel)
        if ok_faces == 6:
            log(f'  [OK] все 6 граней {sky}*.tga')
        elif ok_faces:
            log(f'  [~] найдено {ok_faces}/6 граней — остальные не найдены')
        else:
            log(f'  [??] грани скайбокса {sky} не найдены')

    log(f'Модели, спрайты, звуки: {len(files)} шт.')
    for rel in files:
        src = find_file(rel, dirs, index)
        if src:
            rel2 = copy_into(result_dir, rel, src)
            copied.append(rel2)
            log(f'  [OK] {rel}')
        else:
            missing.append(rel)
            log(f'  [??] НЕ НАЙДЕНО: {rel}')

    # карта и .res
    copy_into(result_dir, f'maps/{mapname}.bsp', bsp_path)
    maps_dir = os.path.join(result_dir, 'maps')
    os.makedirs(maps_dir, exist_ok=True)
    res_items = sorted(set(copied))
    with open(os.path.join(maps_dir, mapname + '.res'), 'w') as f:
        for line in res_items:
            f.write(line + '\n')

    if extract_wad:
        wad_out = os.path.join(result_dir, mapname + '_extracted.wad')
        try:
            cnt = export_wad_from_bsp(bsp_path, wad_out)
            if cnt:
                log(f'Извлечено вшитых текстур: {cnt} -> {wad_out}')
            else:
                log('Вшитых текстур для извлечения нет.')
        except Exception as e:
            log(f'Не удалось извлечь WAD: {e}')

    log('-' * 64)
    log(f'Готово: скопировано {len(copied) + 1}, не найдено {len(missing)}.')
    if missing:
        log('Не найдено (возможно, стандартные ресурсы из valve/):')
        for m_ in missing:
            log(f'  - {m_}')
    log('=' * 64)


# ------------------------------------------------------------------- CLI
def main_cli(argv):
    bsp, mod, valve = None, None, None
    extract = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.lower().endswith('.bsp'):
            bsp = a
        elif a == '--game':
            mod = argv[i + 1]
            i += 1
        elif a == '--valve':
            valve = argv[i + 1]
            i += 1
        elif a == '--extract-wad':
            extract = True
        i += 1
    if not (bsp and mod):
        print(__doc__)
        return 1
    collect_map(bsp, mod, valve, extract_wad=extract)
    return 0


# ------------------------------------------------------------------- GUI
def run_gui():
    root = tk.Tk()
    root.title('PackMap — сборка ресурсов карты CS 1.6')
    root.resizable(False, False)

    frm = ttk.Frame(root, padding=10)
    frm.grid(sticky='nsew')

    bsp_var, mod_var, res_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
    extract_var = tk.BooleanVar(value=False)

    def guess_mod_dir(bsp):
        p = os.path.dirname(os.path.abspath(bsp))
        if os.path.basename(p).lower() == 'maps':
            return os.path.dirname(p)
        return ''

    def default_result(bsp, mod):
        if mod and bsp:
            name = os.path.splitext(os.path.basename(bsp))[0]
            return os.path.join(mod, 'maps', name)
        return ''

    def on_bsp_change(*_):
        bsp = bsp_var.get()
        if bsp and os.path.isfile(bsp):
            mod_var.set(guess_mod_dir(bsp) or mod_var.get())
            res_var.set(default_result(bsp, mod_var.get()))

    bsp_var.trace_add('write', on_bsp_change)
    mod_var.trace_add('write', lambda *_: res_var.set(
        default_result(bsp_var.get(), mod_var.get())))

    def pick_bsp():
        p = filedialog.askopenfilename(title='Выберите BSP-карту',
                                       filetypes=[('BSP карты', '*.bsp')])
        if p:
            bsp_var.set(p)

    def pick_mod():
        p = filedialog.askdirectory(title='Папка мода (например, cstrike)')
        if p:
            mod_var.set(p)

    def pick_res():
        p = filedialog.askdirectory(title='Куда сложить результат')
        if p:
            res_var.set(p)

    rows = [('BSP-файл карты:', bsp_var, pick_bsp),
            ('Папка мода (cstrike):', mod_var, pick_mod),
            ('Папка результата:', res_var, pick_res)]
    for i, (label, var, cmd) in enumerate(rows):
        ttk.Label(frm, text=label).grid(row=i, column=0, sticky='w', pady=2)
        ttk.Entry(frm, textvariable=var, width=58).grid(row=i, column=1, padx=4, pady=2)
        ttk.Button(frm, text='Обзор...', command=cmd).grid(row=i, column=2, pady=2)

    text = tk.Text(frm, height=20, width=90, font=('Consolas', 9))
    text.grid(row=3, column=0, columnspan=3, pady=(8, 4))
    scroll = ttk.Scrollbar(frm, command=text.yview)
    scroll.grid(row=3, column=3, sticky='ns')
    text.config(yscrollcommand=scroll.set)

    # копирование из журнала: правый клик, Ctrl+A, Ctrl+C
    log_menu = tk.Menu(text, tearoff=0)
    log_menu.add_command(label='Копировать',
                         command=lambda: text.event_generate('<<Copy>>'))
    log_menu.add_command(label='Выделить всё',
                         command=lambda: text.tag_add('sel', '1.0', 'end'))
    text.bind('<Button-3>', lambda e: log_menu.tk_popup(e.x_root, e.y_root))
    text.bind('<Control-a>',
              lambda e: (text.tag_add('sel', '1.0', 'end'), 'break')[1])
    text.bind('<Control-c>', lambda e: text.event_generate('<<Copy>>'))

    def do_pack():
        text.delete('1.0', 'end')
        if not (bsp_var.get() and mod_var.get()):
            messagebox.showwarning('PackMap', 'Укажи карту и папку мода.')
            return
        valve = os.path.join(os.path.dirname(mod_var.get().rstrip('\\/')), 'valve')
        try:
            collect_map(bsp_var.get(), mod_var.get(), valve,
                        res_var.get() or None,
                        extract_wad=extract_var.get(),
                        log=lambda s: (text.insert('end', s + '\n'),
                                       text.see('end'), root.update_idletasks()))
        except Exception as e:
            messagebox.showerror('PackMap', str(e))

    def do_extract():
        text.delete('1.0', 'end')
        bsp = bsp_var.get()
        if not (bsp and os.path.isfile(bsp)):
            messagebox.showwarning('PackMap', 'Укажи BSP-файл карты.')
            return
        name = os.path.splitext(os.path.basename(bsp))[0]
        out = os.path.join(os.path.dirname(bsp), name + '_extracted.wad')
        try:
            cnt = export_wad_from_bsp(bsp, out)
            if cnt:
                text.insert('end', f'Извлечено вшитых текстур: {cnt}\nСохранено: {out}\n')
            else:
                text.insert('end', 'Вшитых текстур для извлечения не найдено.\n')
        except Exception as e:
            messagebox.showerror('PackMap', str(e))

    ttk.Checkbutton(frm, text='Извлекать WAD при сборке пакета',
                    variable=extract_var).grid(row=4, column=0, columnspan=2, sticky='w')
    ttk.Button(frm, text='Извлечь WAD', command=do_extract)\
        .grid(row=4, column=2, pady=(4, 0))
    ttk.Button(frm, text='Собрать пакет', command=do_pack)\
        .grid(row=5, column=0, columnspan=3, pady=(4, 0))

    root.mainloop()


def main():
    if len(sys.argv) > 1:
        sys.exit(main_cli(sys.argv[1:]))
    if HAS_TK:
        run_gui()
    else:
        print(__doc__)
        sys.exit('tkinter недоступен — используй режим командной строки')


if __name__ == '__main__':
    main()
