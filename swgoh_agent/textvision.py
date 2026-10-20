"""Local Tesseract text targets; exact words, unique occurrence, real boxes."""
import csv
import io
import re
import subprocess
from typing import NamedTuple

import cv2


class TextMatch(NamedTuple):
    score: float
    point: tuple[int, int] | None
    reason: str = ''


def words(text):
    return re.findall(r'\w+', text.casefold())


class TextMatcher:
    def match(self, image, params):
        x, y, w, h = params.get('region') or [0, 0, image.shape[1], image.shape[0]]
        crop = image[y:y+h, x:x+w]
        primary = self._recognize(crop, params, (x, y))
        # Sparse text layout can lose white labels on bright green buttons.
        # Retry as dark text on white only when no exact occurrence was found;
        # never override ambiguity or an explicitly low-confidence target.
        if primary.reason != 'Text not found':
            return primary
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        white_text = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY_INV)[1]
        return self._recognize(white_text, params, (x, y))

    def _recognize(self, crop, params, origin):
        scaled = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        ok, png = cv2.imencode('.png', scaled)
        if not ok:
            raise ValueError('Cannot encode OCR image')
        try:
            result = subprocess.run(['tesseract', 'stdin', 'stdout', '-l', 'eng', '--psm', '11', 'tsv'],
                                    input=png.tobytes(), capture_output=True, timeout=15, check=True)
        except FileNotFoundError as exc:
            raise RuntimeError('Install tesseract with English data to use text clicks') from exc
        return self.from_tsv(result.stdout.decode('utf-8'), params, origin)

    @staticmethod
    def from_tsv(tsv, params, origin=(0, 0)):
        wanted = words(params['text'])
        if not wanted:
            return TextMatch(0, None, 'Target text has no words')
        lines = {}
        for row in csv.DictReader(io.StringIO(tsv), delimiter='\t'):
            if row.get('level') != '5' or not row.get('text', '').strip():
                continue
            key = tuple(row[k] for k in ('page_num', 'block_num', 'par_num', 'line_num'))
            tokens = words(row['text'])
            for token in tokens:
                lines.setdefault(key, []).append((token, row))
        hits = []
        for line in lines.values():
            for index in range(len(line)-len(wanted)+1):
                chunk = line[index:index+len(wanted)]
                if [item[0] for item in chunk] != wanted:
                    continue
                rows = [item[1] for item in chunk]
                score = min(float(row['conf']) for row in rows) / 100
                left = min(int(row['left']) for row in rows)
                top = min(int(row['top']) for row in rows)
                right = max(int(row['left'])+int(row['width']) for row in rows)
                bottom = max(int(row['top'])+int(row['height']) for row in rows)
                hits.append((score, (origin[0]+round((left+right)/4),
                                     origin[1]+round((top+bottom)/4))))
        # Count every exact occurrence, even a low-confidence duplicate.
        if len(hits) != 1:
            return TextMatch(0, None, 'Text not found' if not hits else 'Text is ambiguous; select a smaller OCR region')
        score, point = hits[0]
        if score < params['min_text_confidence']:
            return TextMatch(score, None, 'OCR confidence is below the threshold')
        return TextMatch(score, point)
