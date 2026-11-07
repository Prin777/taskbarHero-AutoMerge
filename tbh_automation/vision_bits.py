"""Exact white-pixel bit rows; x=0 is the least significant bit."""
from PIL import Image, ImageChops
_REVERSE=bytes(int(f'{value:08b}'[::-1],2) for value in range(256))

def white_pixel_rows(rgb,checkpoint=lambda:None):
    checkpoint()
    r,g,b=rgb.split()
    mask=ImageChops.darker(ImageChops.darker(r,g),b).point(lambda p:255 if p>=175 else 0)
    data=mask.convert('1',dither=Image.Dither.NONE).tobytes()
    stride=(rgb.width+7)//8
    rows=[]
    for y in range(rgb.height):
        if y%64==0:checkpoint()
        rows.append(int.from_bytes(data[y*stride:(y+1)*stride].translate(_REVERSE),'little'))
    checkpoint()
    return rows
