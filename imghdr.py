# Minimal stub so older libraries that import 'imghdr' don't crash on Python 3.13+
# Our bot doesn't upload image files, so simple None detection is fine.
def what(file, h=None):
    return None
