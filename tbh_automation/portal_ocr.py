"""Windows built-in OCR, in-memory only; no diagnostic screenshots are written."""
import base64
import io
import json
import subprocess
import re
import time

_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.InMemoryRandomAccessStream,Windows.Foundation,ContentType=WindowsRuntime]
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
function Await($operation, $type) {
 $task = $asTask.MakeGenericMethod($type).Invoke($null, @($operation))
 $task.GetAwaiter().GetResult()
}
$bytes = [Convert]::FromBase64String([Console]::ReadLine())
$stream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
$writer = New-Object Windows.Storage.Streams.DataWriter($stream)
$writer.WriteBytes($bytes)
$null = Await ($writer.StoreAsync()) ([uint32])
$stream.Seek(0)
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$null = [Windows.Globalization.Language,Windows.Foundation,ContentType=WindowsRuntime]
$language = [Windows.Globalization.Language]::new('ja')
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if ($null -eq $engine) { throw 'Windows OCR language unavailable' }
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
[Console]::WriteLine((ConvertTo-Json -Compress -InputObject $result.Text))
$bitmap.Dispose()
$writer.Dispose()
$stream.Dispose()
'''
_COMMAND = base64.b64encode(_SCRIPT.encode('utf-16le')).decode('ascii')

_WORDS_SCRIPT = _SCRIPT.replace(
    '[Console]::WriteLine((ConvertTo-Json -Compress -InputObject $result.Text))',
    '$lines = @($result.Lines | ForEach-Object { $line = $_; @($line.Words | ForEach-Object { @{text=$_.Text; x=$_.BoundingRect.X; y=$_.BoundingRect.Y; width=$_.BoundingRect.Width; height=$_.BoundingRect.Height} }) }); [Console]::WriteLine((ConvertTo-Json -Depth 6 -Compress -InputObject $lines))'
)
_WORDS_COMMAND = base64.b64encode(_WORDS_SCRIPT.encode('utf-16le')).decode('ascii')

# Reuse Windows OCR initialization for every image in a frame's batch.
_setup = _WORDS_SCRIPT[:_WORDS_SCRIPT.index('$bytes =')]
_engine_start = _WORDS_SCRIPT.index('$null = [Windows.Globalization.Language')
_result_start = _WORDS_SCRIPT.index('$result = Await')
_engine = _WORDS_SCRIPT[_engine_start:_result_start]
_body = _WORDS_SCRIPT[_WORDS_SCRIPT.index('$bytes ='):_engine_start]
_body = _body.replace('[Console]::ReadLine()', '$encoded')
_tail = _WORDS_SCRIPT[_result_start:]
_tail = _tail.replace('[Console]::WriteLine((ConvertTo-Json -Depth 6 -Compress -InputObject $lines))', '$outputs.Add($lines)')
_BATCH_SCRIPT = (_setup + _engine +
    '$images = ConvertFrom-Json ([Console]::ReadLine()); $outputs = [System.Collections.Generic.List[object]]::new(); foreach ($encoded in $images) {\n' +
    _body + _tail + '\n}; [Console]::WriteLine((ConvertTo-Json -Depth 8 -Compress -InputObject $outputs.ToArray()))')
_BATCH_COMMAND = base64.b64encode(_BATCH_SCRIPT.encode('utf-16le')).decode('ascii')


def parse_count(text):
    # No character guessing: an ambiguous glyph must never grant a click.
    match = re.fullmatch(r'\s*([0-9]{1,6})\s*/\s*1\s*', text)
    if not match:
        return None
    digits = match[1]
    if len(digits)>1 and digits.startswith('0'):
        return None
    return int(digits)


def _ocr_result(image, checkpoint, command=_COMMAND):
    images=image if isinstance(image,list) else [image]
    encoded=[]
    for item in images:
        checkpoint()
        buffer=io.BytesIO()
        item.save(buffer,format='PNG')
        encoded.append(base64.b64encode(buffer.getvalue()).decode('ascii'))
    process = subprocess.Popen(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', command],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
    )
    payload = (json.dumps(encoded) if isinstance(image,list) else encoded[0])+'\n'
    deadline = time.monotonic()+12
    try:
        while True:
            checkpoint()
            if time.monotonic() >= deadline:
                raise RuntimeError('Windows 数字识别超时，已停止自动挑战')
            try:
                output, _error = process.communicate(input=payload, timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                payload = None
        checkpoint()
        if process.returncode:
            raise RuntimeError('Windows 数字识别不可用，已停止自动挑战')
        return json.loads(output.strip())
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def _read_once(image, checkpoint, *, context=False):
    text = _ocr_result(image, checkpoint)
    if context:
        match = re.fullmatch(r'\s*1234567890\s+(.+)', text)
        if not match:
            return None
        text = match[1]
    return parse_count(text)


def read_words(image, checkpoint=lambda:None):
    return _ocr_result(image,checkpoint,_WORDS_COMMAND)


def read_words_batch(images,checkpoint=lambda:None,language=None):
    if not images:return []
    command=_BATCH_COMMAND
    if language is not None:
        if language!='zh-Hans-CN':raise ValueError('Unsupported OCR language')
        script=_BATCH_SCRIPT.replace("::new('ja')","::new('zh-Hans-CN')")
        command=base64.b64encode(script.encode('utf-16le')).decode('ascii')
    result=_ocr_result(images,checkpoint,command)
    if not isinstance(result,list) or len(result)!=len(images) or any(not isinstance(words,list) for words in result):
        raise RuntimeError('批量 OCR 返回格式不完整，已取消识别')
    return result


def read_count(image, checkpoint=lambda: None):
    from PIL import Image
    count = _read_once(image, checkpoint)
    if count is not None:
        return count
    # Windows OCR is sensitive to the raster size of tiny game fonts.
    smaller = image.resize((round(image.width*0.75), round(image.height*0.75)), Image.Resampling.BICUBIC)
    count = _read_once(smaller, checkpoint)
    if count is not None:
        return count
    # Provide a fixed numeral context, which improves Windows OCR on isolated labels.
    # The prefix is required verbatim in the result and is never part of the count.
    from PIL import ImageDraw, ImageFont, ImageOps
    import os
    dark = image.convert('L').point(lambda p: 255 if p<100 else 0)
    box = dark.getbbox()
    if box is None:
        return None
    glyphs = image.crop(box)
    glyphs = glyphs.resize((max(1,round(glyphs.width*46/glyphs.height)),46),Image.Resampling.BICUBIC)
    font = ImageFont.truetype(os.path.join(os.environ.get('WINDIR','C:/Windows'),'Fonts','arial.ttf'),48)
    prefix = '1234567890'
    prefix_width = round(font.getlength(prefix))
    canvas = Image.new('RGB',(prefix_width+glyphs.width+70,90),'white')
    ImageDraw.Draw(canvas).text((20,11),prefix,font=font,fill='black')
    canvas.paste(glyphs,(prefix_width+45,20))
    return _read_once(canvas, checkpoint, context=True)
