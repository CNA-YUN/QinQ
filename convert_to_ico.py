from PIL import Image

# 打开你已有的图片（可以是 PNG，JPG 等）
img = Image.open("yz.jpg")

# 将其保存为 ICO 文件，并指定包含的多种尺寸
img.save("yz.ico", format='ICO', sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])