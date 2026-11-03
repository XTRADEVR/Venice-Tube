# Venice Tub - Streamlit Downloader

Web app downloader TikTok, Instagram, YouTube, Facebook built with Streamlit. Tampilan bersih, no ribet, bisa jalan di HP / Streamlit Cloud.

![Streamlit](https://img.shields.io/badge/Streamlit-FF4B4B?style=for-the-badge&logo=Streamlit&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)

## ✨ Fitur
- Download TikTok (no watermark), IG Reels, YouTube, FB Video
- UI Streamlit yang clean
- Support `yt-dlp` engine - anti gagal
- Auto clean storage
- Siap deploy ke Streamlit Cloud / Docker

## 📁 File
- `app.py` - main Streamlit app
- `downloader.py` - core downloader logic
- `storage.py` - manajemen file temp
- `config.py` - config & settings
- `requirements.txt`
- `Dockerfile` + `run.sh` buat VPS

## 🚀 Cara Jalanin Lokal

```bash
pip install -r requirements.txt
streamlit run app.py
