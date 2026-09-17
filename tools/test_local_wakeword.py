import os, sys, time, pyaudio
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, 'backend'))
from wakeword_engine import WakeWordEngine

def main():
    print('\n' + '=' * 65)
    print(' 🎙️ ЛОКАЛЬНАЯ ОТЛАДКА ВЕЙКВОРДА «ПОРФИРИЙ» (openWakeWord)')
    print('=' * 65)
    model_path = os.path.join(BASE_DIR, 'porfiriy.onnx')
    if not os.path.exists(model_path):
        model_path = os.path.join(BASE_DIR, 'backend', 'porfiriy.onnx')
    engine = WakeWordEngine(model_path=model_path, threshold=0.60)
    if not engine.is_ready:
        print(f'[!] ОШИБКА: Модель вейкворда не загружена ({model_path})')
        return
    p = pyaudio.PyAudio()
    try:
        stream = p.open(format=pyaudio.paInt16, channels=1, rate=16000, input=True, frames_per_buffer=1024)
    except Exception as e:
        print(f'[!] Ошибка открытия микрофона: {e}')
        p.terminate()
        return
    print('✔ Микрофон открыт (16000 Гц, Моно).')
    print('✔ Произнесите в микрофон: «Порфирий»... (Выход: Ctrl+C)\n')
    try:
        while True:
            data = stream.read(1024, exception_on_overflow=False)
            if not data: continue
            score, peak, rms = engine.process_chunk(data, 'local_test')
            bar = '█' * int(round(score * 25)) + '░' * (25 - int(round(score * 25)))
            status = '🔴 ЖДУ...' if score < 0.20 else ('🟡 РАСПОЗНАЕТСЯ...' if score < 0.60 else '🎉 СРАБОТКА!')
            sys.stdout.write(f'\r[{bar}] Скор: {score:5.3f} | Пик: {peak:5.3f} | RMS: {rms:5.1f} dBFS | {status:<18}')
            sys.stdout.flush()
            time.sleep(0.01)
    except KeyboardInterrupt:
        print('\n\nТест остановлен.')
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()

if __name__ == '__main__':
    main()
