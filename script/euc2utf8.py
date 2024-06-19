import pandas as pd

# 원본
input_file = 'gg.csv'
input_encoding = 'euc-kr'

# 출력
output_file = 'ggd.csv'
output_encoding = 'utf-8'

df = pd.read_csv(input_file, encoding=input_encoding)

df.to_csv(output_file, encoding=output_encoding, index=False)