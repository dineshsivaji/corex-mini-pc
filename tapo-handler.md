
  Countdown timer (device-side):
  import asyncio
  from kasa import Discover, Credentials

  async def main():
      dev = await Discover.discover_single("192.168.1.XXX", credentials=Credentials("your_email", "your_password"))
      await dev.update()

      # Turn off after 300 seconds (device counts down even if Python exits)
      await dev._query_helper("add_countdown_rule", {
          "delay": 300,
          "desired_states": {"on": False},
          "enable": True,
          "remain": 300
      })

  asyncio.run(main())

  Recurring schedule (device-side):
  # Turn on at 11:50am every day
  await dev._query_helper("add_schedule_rule", {
      "name": "morning_on",
      "enable": True,
      "week_day": 127,          # bitmask: Mon-Sun = 127
      "s_min": 710,             # minutes from midnight (11*60 + 50)
      "s_type": "normal",
      "e_min": 0,
      "e_type": "normal",
      "e_action": "none",
      "desired_states": {"on": True},
      "mode": "repeat",         # "repeat" or "once"
  })

  Read/delete existing schedules:
  # List
  rules = await dev._query_helper("get_schedule_rules", {"start_index": 0})

  # Delete all
  await dev._query_helper("remove_schedule_rules", {"id_list": ["S1", "S2"]})

  The device stores up to 32 schedule rules and 1 countdown. They persist exactly as if set via the Tapo app.
