import os

def convert_tflite_to_c_array(tflite_path, header_path):
    with open(tflite_path, "rb") as f:
        tflite_data = f.read()
    
    with open(header_path, "w") as f:
        f.write("#ifndef PORFIRIY_MODEL_H\n")
        f.write("#define PORFIRIY_MODEL_H\n\n")
        f.write("unsigned char porfiriy_tflite[] = {\n")
        
        for i, byte in enumerate(tflite_data):
            if i % 12 == 0:
                f.write("  ")
            f.write(f"0x{byte:02x}, ")
            if (i + 1) % 12 == 0:
                f.write("\n")
                
        f.write("\n};\n")
        f.write(f"unsigned int porfiriy_tflite_len = {len(tflite_data)};\n\n")
        f.write("#endif // PORFIRIY_MODEL_H\n")

if __name__ == "__main__":
    tflite_path = "porfiriy.tflite"
    header_path = "esp32_firmware/src/model.h"
    if os.path.exists(tflite_path):
        convert_tflite_to_c_array(tflite_path, header_path)
        print(f"Successfully converted {tflite_path} to {header_path}")
    else:
        print(f"Error: {tflite_path} not found!")
