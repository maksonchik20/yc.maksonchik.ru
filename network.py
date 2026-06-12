import requests


url = "https://yc.maksonchik.ru"
query = input("What do you want? Enter 'send' or 'get': ")
if query == "get":
    resp = requests.post(f"{url}/get/").json()
    for message in resp["Messages"]:
        print(message)
else:
    message = input()
    resp = requests.post(f"{url}/send/", json={"message": message})
    print(resp.content)
