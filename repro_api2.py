import json
from app import web, xunleipan

RAW = json.dumps({
    "token_type": "Bearer",
    "access_token": "eyJhbGciOiJSUzI1NiIsImtpZCI6ImU1MzIyMDgyLTVjMzMtNDgzOS1hY2EwLWJlZWJhNDVmNzA4YyJ9.eyJpc3MiOiJodHRwczovL3hsdXNlci1zc2wueHVubGVpLmNvbSIsInN1YiI6IjgxNDkyNjUyOCIsImF1ZCI6IlhxcDBrSkJYV2h3YVRwQjYiLCJleHAiOjE3OTI4OTEwODksImlhdCI6MTc5Mjg0Nzg4OSwiYXRfaGFzaCI6InIuS3lzNDFCN0pTWU91MElwRlJfZ3NPUSIsInNjb3BlIjoicHJvZmlsZSBwYW4gc3NvIHVzZXIiLCJwcm9qZWN0X2lkIjoiMnJ2azRlM2drZG5sN3Uxa2wwayIsIm1ldGEiOnsiYSI6IkUxWlowVXdjeUlSdkhlbkk0cUcxQnVaTnI2dTdISXNZYkRwaGVnTXVCS0xMZTdLL2xoNkIxdytpNC9yT3dBRWFJaU95SFhmY2ZZMEFxbG5taE91a1hRPT0ifX0.BtdtO1JEG7GJdrZ2UpKKR4Q5hNklTJMLZGzmE-s0bYqXYOjkM7bAj8M4ZZKXmn0CqLZOiSXgsYF5tzbn9CrcSEU15REWJMEH1nVWVgRdCSlMQG2SoGfJCuerXUZHK21mg6RSOlwqLZ02UTaOX6ietxCOy8eT5wOD_HJlkL9u4e2rJ-6m5A_8cEeRdM2Of3anaVvIwRmPkjIYCjBa5FWf_1cBNCnfg9paeNRcrb_0aWrxiEcWHKjjGgAiq8Gfgs5NezYt92-0Ir8gdFBIfAnjxxx68eH_Otpg4lVk6VY25yW-8cu6XvfHWnqK0qtWinW2qYChftokokBNcaHr2xbfdQ",
    "refresh_token": "a1.z906yV8ciGrcsBEY7-6IUZO8xS4ifiXe_GMsUrVdRn33kVRfmCvpdYFHGbc",
    "expires_in": 43200, "sub": "814926528", "user_id": "814926528",
    "expires_at": "2026-10-25T01:17:28.306Z"})

out = web.xlp_apply_credentials(RAW)
print("ok       =", out.get("ok"))
print("saved    =", out.get("saved"))
print("missing  =", out.get("missing"))
print("detail   =", out.get("detail"))
print("error    =", (out.get("error") or "")[:160])
print("剩余     =", out.get("expires_in"), "秒 | has_refresh =", out.get("has_refresh"))
