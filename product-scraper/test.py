import pandas as pd
df = pd.read_csv("heyetsy.com-listings-images.csv", dtype=str, header=None, engine="python", on_bad_lines="skip").fillna("")
row = df[df[0] == "4312327290"]
print(row.to_string())