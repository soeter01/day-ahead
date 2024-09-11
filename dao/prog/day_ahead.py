"""
Het programma Day Ahead Optimalisatie kun je je energieverbruik en
energiekosten optimaliseren als je gebruik maakt van dynamische prijzen.
Zie verder: DOCS.md
"""
import datetime
import datetime as dt
import sys
import math
import pandas as pd
from mip import Model, xsum, minimize, BINARY, CONTINUOUS
from utils import (get_value_from_dict, is_laagtarief, convert_timestr,
                   calc_uur_index, error_handling, calc_adjustment_heatcurve)
import logging
from da_base import DaBase


class DaCalc(DaBase):

    def __init__(self, file_name=None):
        super().__init__(file_name=file_name)
        self.history_options = self.config.get(["history"])
        self.boiler_options = self.config.get(["boiler"])
        self.battery_options = self.config.get(["battery"])
        self.prices_options = self.config.get(["prices"])
        self.ev_options = self.config.get(["electric vehicle"])
        self.heating_options = self.config.get(["heating"])
        self.use_calc_baseload = (self.config.get(["use_calc_baseload"], None,
                                                  "false").lower() == "true")
        self.heater_present = False
        self.boiler_present = False
        self.grid_max_power = self.config.get(["grid", "max_power"], None, 17)
        self.machines = self.config.get(["machines"], None, [])
        # self.start_logging()

    def calc_optimum(self, _start_dt: dt.datetime | None = None,
                     _start_soc: float | None = None):
        if _start_dt is not None or _start_soc is not None:
            self.debug = True
        logging.info(f"Debug = {self.debug}")
        if _start_dt is None:
            start_dt = dt.datetime.now()
        else:
            start_dt = _start_dt
        start_ts = int(start_dt.timestamp())
        modulo = start_ts % 3600
        if modulo > 3550:
            start_ts = start_ts + 3600 - modulo
        start_dt = dt.datetime.fromtimestamp(start_ts)
        start_h = int(3600 * math.floor(start_ts / 3600))
        fraction_first_hour = 1 - (start_ts - start_h) / 3600
        prog_data = self.db_da.get_prognose_data(start=start_h, end=None)
        # start = dt.datetime.timestamp(dt.datetime.strptime("2022-05-27",
        # "%Y-%m-%d"))
        # end = dt.datetime.timestamp(dt.datetime.strptime("2022-05-29",
        # "%Y-%m-%d"))
        # prog_data = db_da.get_prognose_data(start, end)
        u = len(prog_data)
        if u <= 2:
            logging.error("Er ontbreken voor een aantal uur gegevens "
                          "(meteo en/of dynamische prijzen)\n",
                          "er kan niet worden gerekend")
            if self.notification_entity is not None:
                self.set_value(self.notification_entity,
                               "Er ontbreken voor een aantal uur gegevens; "
                               "er kan niet worden gerekend")
            return
        if u <= 8:
            logging.warning("Er ontbreken voor een aantal uur gegevens "
                            "(meteo en/of dynamische prijzen)\n",
                            "controleer of alle gegevens zijn opgehaald")
            if self.notification_entity is not None:
                self.set_value(self.notification_entity,
                               "Er ontbreken voor een aantal uur gegevens")

        if (self.notification_entity is not None and
                self.notification_berekening):
            self.set_value(self.notification_entity, "DAO calc gestart " +
                           dt.datetime.now().
                           strftime('%d-%m-%Y %H:%M:%S'))
        logging.debug('Prognose data:\n{}'.format(prog_data.to_string()))

        '''
        day_ahead prijs omrekenen naar twee prijzen
        1. pl: prijs voor verbruik (levering)
            altijd met opslag voor 
            eb_l 0,12599 (2023)
            opslag leverancier, ol_l 0,001 (tibber)
            btw over het geheel 21%
        2. pt: prijs voor teruglevering
            alleen opslag voor saldering, 
            na 6 maanden saldo levering/teruglevering , als teruglevering > 
            levering dan geen opslag eb en ode
            eb_t 0,12955
            opslag leverancier ol_t (aftrek!!) 0,0
            en btw over het geheel 21%
        '''
        taxes_l_def = self.prices_options["energy taxes delivery"]
        # eb + ode levering
        # eb_l = 0.12955
        # opslag kosten leverancier
        ol_l_def = self.prices_options["cost supplier delivery"]
        # ol_l_def ["2022-01-01] = 0.002
        # ol_l_def ["2023-03-01] = 0.018
        # eb+ode teruglevering
        taxes_t_def = self.prices_options["energy taxes redelivery"]
        # eb_t = 0.12955
        # eb_t = 0
        # ode_t = 0
        ol_t_def = self.prices_options["cost supplier redelivery"]
        # ol_t = 0 #-0.011
        btw_def = self.prices_options["vat"]
        # btw = 0.09

        # prijzen van een traditionele leverancier zijn alleen indicatief;
        # er wordt niet mee gerekend
        gc_p_low = self.prices_options['regular low']
        gc_p_high = self.prices_options['regular high']
        pl = []  # prijs levering day_ahead
        pt = []  # prijs teruglevering day_ahead
        pl_avg = []  # prijs levering day_ahead gemiddeld
        pt_notax = []  # prijs teruglevering day ahead zonder taxes
        uur = []  # datum_tijd van het betreffende uur
        prog_data = prog_data.reset_index()
        # make sure indexes pair with number of rows
        for row in prog_data.itertuples():
            uur.append(row.tijd)
            dag_str = row.tijd.strftime("%Y-%m-%d")
            ol_l = get_value_from_dict(dag_str, ol_l_def)
            ol_t = get_value_from_dict(dag_str, ol_t_def)
            taxes_l = get_value_from_dict(dag_str, taxes_l_def)
            taxes_t = get_value_from_dict(dag_str, taxes_t_def)
            btw = get_value_from_dict(dag_str, btw_def)
            price_l = round((row.da_price + taxes_l + ol_l) *
                            (1 + btw / 100), 5)
            price_t = round((row.da_price + taxes_t + ol_t) *
                            (1 + btw / 100), 5)
            pl.append(price_l)
            pt.append(price_t)
            # tarief teruglevering zonder eb en btw
            price_t_notax = row.da_price
            pt_notax.append(price_t_notax)

        B = len(self.battery_options)
        U = len(pl)
        if U >= 24:
            p_avg = sum(pl) / U  # max(pl) #
        else:
            dag_str = dt.datetime.now().strftime("%Y-%m-%d")
            ol_l = get_value_from_dict(dag_str, ol_l_def)
            taxes_l = get_value_from_dict(dag_str, taxes_l_def)
            btw = get_value_from_dict(dag_str, btw_def)
            p_avg = (self.calc_da_avg() + taxes_l + ol_l) * (1 + btw / 100)

        for u in range(U):
            pl_avg.append(p_avg)

        # base load
        if self.use_calc_baseload:
            logging.info(f"Zelf berekende baseload")
            weekday = dt.datetime.weekday(dt.datetime.now())
            base_cons = self.get_calculated_baseload(weekday)
            if U > 24:
                # volgende dag ophalen
                weekday += 1
                weekday = weekday % 7
                base_cons = base_cons + self.get_calculated_baseload(weekday)
        else:
            logging.info(f"Baseload uit instellingen")
            base_cons = self.config.get(["baseload"])
            if U >= 24:
                base_cons = base_cons + base_cons

        # 0.015 kWh/J/cm² productie van mijn panelen per J/cm²
        pv_yield = []
        solar_prod = []
        entity_pv_ac_switch = []
        solar_num = len(self.solar)
        for s in range(solar_num):
            pv_yield.append(float(self.config.get(["yield"], self.solar[s])))
            solar_prod.append([])
            entity = self.config.get(["entity pv switch"], self.solar[s], None)
            if entity == "":
                entity = None
            entity_pv_ac_switch.append(entity)

        time_first_hour = dt.datetime.fromtimestamp(prog_data["time"].iloc[0])
        first_hour = int(time_first_hour.hour)
        b_l = base_cons[first_hour:]
        uur = []  # hulparray met uren
        tijd = []
        ts = []
        global_rad = []  # globale straling per uur
        pv_org_ac = []  # opwekking zonnepanelen[]
        pv_org_dc = []
        p_grl = []  # prijs levering
        p_grt = []  # prijs teruglevering
        hour_fraction = []
        first_hour = True

        prog_data = prog_data.reset_index()
        # make sure indexes pair with number of rows
        for row in prog_data.itertuples():
            dtime = dt.datetime.fromtimestamp(row.time)
            hour = int(dtime.hour)
            uur.append(hour)
            tijd.append(dtime)
            global_rad.append(row.glob_rad)
            pv_total = 0
            if first_hour:
                ts.append(start_ts)
                hour_fraction.append(fraction_first_hour)
                # pv.append(pv_total * fraction_first_hour)
            else:
                ts.append(row.time)
                hour_fraction.append(1)
                # pv.append(pv_total)
            for s in range(solar_num):
                prod = (self.meteo.calc_solar_rad(
                    self.solar[s], row.time, row.glob_rad) * pv_yield[s] *
                        hour_fraction[-1])
                solar_prod[s].append(prod)
                pv_total += prod
            pv_org_ac.append(pv_total)
            pv_total = 0
            for b in range(B):
                for s in range(len(self.battery_options[b]["solar"])):
                    prod = (self.meteo.calc_solar_rad(
                        self.battery_options[b]["solar"][s], row.time,
                        row.glob_rad) *
                        self.battery_options[b]["solar"][s]["yield"] *
                        hour_fraction[-1])
                    pv_total += prod
            pv_org_dc.append(pv_total)

            dag_str = dtime.strftime("%Y-%m-%d")
            taxes_l = get_value_from_dict(dag_str, taxes_l_def)
            taxes_t = get_value_from_dict(dag_str, taxes_t_def)
            btw = get_value_from_dict(dag_str, btw_def)
            if is_laagtarief(
                    dt.datetime(dtime.year, dtime.month, dtime.day, hour),
                    self.config.get(["switch to low"], self.prices_options,
                                    23)):
                p_grl.append((gc_p_low + taxes_l) * (1 + btw / 100))
                p_grt.append((gc_p_low + taxes_t) * (1 + btw / 100))
            else:
                p_grl.append((gc_p_high + taxes_l) * (1 + btw / 100))
                p_grt.append((gc_p_high + taxes_t) * (1 + btw / 100))
            first_hour = False
        try:
            if self.log_level == logging.INFO:
                start_df = pd.DataFrame(
                    {"uur": uur,
                     "tijd": tijd,
                     'p_l': pl,
                     'p_t': pt,
                     'base': b_l,
                     'pv_ac': pv_org_ac,
                     'pv_dc': pv_org_dc
                     })
                start_df.set_index('uur')
                logging.info(f"Start waarden: \n{start_df.to_string()}")
        except Exception as ex:
            logging.error(ex)
        logging.debug(f"lengte prognose arrays:")
        logging.debug(f"uur: {len(uur)}")
        logging.debug(f"tijd: {len(tijd)}")
        logging.debug(f"p_l: {len(pl)}")
        logging.debug(f"p_t: {len(pt)}")
        logging.debug(f"base: {len(b_l)}")
        logging.debug(f"pv_ac: {len(pv_org_ac)}")
        logging.debug(f"pv_ac: {len(pv_org_dc)}")

        # volledig salderen?
        salderen = self.prices_options['tax refund'] == "True"

        last_invoice = dt.datetime.strptime(
            self.prices_options['last invoice'], "%Y-%m-%d")
        cons_data_history = (
            self.db_da.get_consumption(last_invoice, dt.datetime.today()))
        logging.info(f"Verbruik dit contractjaar: "
                     f"{cons_data_history['consumption']}")
        logging.info(f"Productie dit contractjaar: "
                     f"{cons_data_history['production']}")
        if not salderen:
            salderen = (
                    cons_data_history["production"] <
                    cons_data_history["consumption"])

        if salderen:
            logging.info(f"All taxes refund (alles wordt gesaldeerd)")
            consumption_today = 0
            production_today = 0
        else:
            consumption_today = (
                float(self.get_state("sensor.daily_grid_consumption").state))
            production_today = (
                float(self.get_state("sensor.daily_grid_production").state))
            logging.info(f"consumption today: {consumption_today} kWh")
            logging.info(f"production today: {production_today} kWh")
            logging.info(f"verschil: "
                         f"{consumption_today - production_today} kWh")

        model = Model()

        # reken met prijzen traditionele leverancier
        # pl = p_grl
        # pt = p_grt

        ##############################################################
        #                          pv ac
        ##############################################################
        pv_ac = [[model.add_var(var_type=CONTINUOUS, lb=0,
                                ub=solar_prod[s][u] * 1.1)
                  for u in range(U)] for s in range(solar_num)]
        pv_ac_on_off = [[model.add_var(var_type=BINARY)
                         for _ in range(U)] for _ in range(solar_num)]

        # constraints

        for s in range(solar_num):
            for u in range(U):
                model += pv_ac[s][u] == solar_prod[s][u] * pv_ac_on_off[s][u]
        for s in range(solar_num):
            if entity_pv_ac_switch[s] is None:
                for u in range(U):
                    model += pv_ac_on_off[s][u] == 1

        ##############################################################
        #                          accu / batterij
        ##############################################################
        # accu capaciteit
        # 2 batterijen 50V 280Ah
        one_soc = []
        kwh_cycle_cost = []
        start_soc = []
        opt_low_level = []
        # pv_dc = []  # pv bruto productie per batterij per uur
        # pv_dc_hour_sum = []
        # pv_from_dc_hour_sum = []
        #   de som van pv_dc productie geleverd aan ac per uur
        # eff_ac_to_dc = []
        # eff_dc_to_ac = []
        eff_dc_to_bat = []
        eff_bat_to_dc = []
        # max_ac_to_dc = []
        # max_dc_to_ac = []

        CS = []
        DS = []
        max_charge_power = []
        max_discharge_power = []
        max_dc_from_bat_power = []
        max_dc_to_bat_power = []
        avg_eff_dc_to_ac = []
        pv_dc_num = []
        pv_prod_dc = []
        pv_prod_ac = []
        for b in range(B):
            pv_prod_ac.append([])
            pv_prod_dc.append([])
            # noinspection PyTypeChecker
            max_charge_power.append(int(
                self.battery_options[b]["charge stages"][-1]["power"])/1000)
            # CS is aantal charge stages
            CS.append(len(self.battery_options[b]["charge stages"]))
            max_discharge_power.append(
                self.battery_options[b]["discharge stages"][-1]["power"]/1000)
            max_dc_from_bat_power.append(
                self.config.get(["bat_to_dc max power"],
                                self.battery_options[b],
                                2000 * max_discharge_power[b])/1000)
            max_dc_to_bat_power.append(
                self.config.get(["dc_to_bat max power"],
                                self.battery_options[b],
                                2000 * max_discharge_power[b])/1000)
            # DS is aantal discharge stages
            DS.append(len(self.battery_options[b]["discharge stages"]))
            sum_eff = 0
            for ds in range(DS[b])[1:]:
                sum_eff += self.battery_options[b]["discharge stages"][
                    ds]["efficiency"]
            avg_eff_dc_to_ac.append(sum_eff/(DS[b]-1))

            ac = float(self.battery_options[b]["capacity"])
            one_soc.append(ac / 100)  # 1% van 28 kWh = 0,28 kWh
            kwh_cycle_cost.append(self.battery_options[b]["cycle cost"])
            logging.debug(f"cycle cost: {kwh_cycle_cost[b]} eur/kWh")

            eff_dc_to_bat.append(float(self.battery_options[b][
                                           "dc_to_bat efficiency"]))
            # fractie van 1
            eff_bat_to_dc.append(float(self.battery_options[b][
                                           "bat_to_dc efficiency"]))
            # fractie van 1

            if _start_soc is None or b > 0:
                start_soc_str = self.get_state(self.battery_options[b]["entity actual level"]).state
                if start_soc_str.lower() == "unavailable":
                    start_soc.append(50)
                else:
                    start_soc.append(float(start_soc_str))
            else:
                start_soc.append(_start_soc)
            logging.info(f"Startwaarde SoC {self.battery_options[b]['name']}: {start_soc[b]}%")
            opt_low_level.append(float(self.battery_options[b]["optimal lower level"]))

            # pv dc mppt
            pv_dc_num.append(len(self.battery_options[b]["solar"]))
            # pv_dc_bat = []
            for s in range(pv_dc_num[b]):
                pv_prod_dc[b].append([])
                pv_prod_ac[b].append([])
                pv_yield = self.battery_options[b]["solar"][s]["yield"]
                for u in range(U):
                    # pv_prod productie van batterij b van solar s in uur u
                    prod_dc = self.meteo.calc_solar_rad(self.battery_options[b]["solar"][s],
                                                        int(tijd[u].timestamp()),
                                                        global_rad[u]) * pv_yield
                    eff = 1
                    for ds in range(DS[b]):
                        if self.battery_options[b]["discharge stages"][ds]["power"]/1000 > prod_dc:
                            eff = self.battery_options[b]["discharge stages"][ds]["efficiency"]
                            break
                    prod_ac = prod_dc * eff
                    pv_prod_dc[b][s].append(prod_dc)
                    pv_prod_ac[b][s].append(prod_ac)

        # energie per uur, vanuit dc gezien
        # ac_to_dc = [[model.add_var(var_type=CONTINUOUS, lb=0, ub=hour_fraction[u] * \
        #                max_ac_to_dc[b]) for u in range(U)] for b in range(B) ]
        # hernoemd naar dc_from_ac
        # totaal elektra van ac naar de busbar, ieder uur

        # alle variabelen definieren alles in W tenzij aangegeven
        # mppt aan/uit evt bij netto prijzen onder nul
        pv_dc_on_off = [[[model.add_var(var_type=BINARY) for _ in range(U)]
                        for _ in range(pv_dc_num[b])] for b in range(B)]
        pv_prod_dc_sum = [[model.add_var(var_type=CONTINUOUS, lb=0, ub=2 * max_charge_power[b])
                          for _ in range(U)] for b in range(B)]

        # ac_to_dc met aan uit #############################################################
        '''
        #ac_to_dc: wat er gaat er vanuit ac naar de omvormer
        ac_to_dc = [[model.add_var(var_type=CONTINUOUS, lb=0, ub=max_charge_power[b])
                     for u in range(U)] for b in range(B)]
        ac_to_dc_on = [[model.add_var(var_type=BINARY) for u in range(U)] for b in range(B)]

        # elektra per vermogensklasse van ac naar de busbar, ieder uur
        ac_to_dc_st = [[[model.add_var(var_type=CONTINUOUS, lb=0,
                        ub=self.battery_options[b]["charge stages"][cs]["power"]/1000)
                        for u in range(U)] for cs in range(CS[b])] for b in range(B)]
        # vermogens klasse aan/uit
        ac_to_dc_st_on = [[[model.add_var(var_type=BINARY)
            for u in range(U)] for cs in range(CS[b])] for b in range(B)]
        '''
        # met sos ###################################################################
        ac_to_dc_samples = [[self.battery_options[b]["charge stages"][cs]["power"]/1000
                            for cs in range(CS[b])] for b in range(B)]
        dc_from_ac_samples = [[(self.battery_options[b]["charge stages"][cs]["efficiency"] *
                               self.battery_options[b]["charge stages"][cs]["power"] / 1000)
                               for cs in range(CS[b])] for b in range(B)]
        ac_to_dc = [[model.add_var(var_type=CONTINUOUS, lb=0, ub=max_charge_power[b])
                     for _ in range(U)] for b in range(B)]
        ac_to_dc_on = [[model.add_var(var_type=BINARY) for _ in range(U)] for _ in range(B)]
        ac_to_dc_w = [[[model.add_var(var_type=CONTINUOUS, lb=0, ub=1)
                        for _ in range(CS[b])] for _ in range(U)] for b in range(B)]
        # tot hier met sos
        # '''
        ac_from_dc = [[model.add_var(var_type=CONTINUOUS, lb=0, ub=max_discharge_power[b])
                       for _ in range(U)] for b in range(B)]
        ac_from_dc_on = [[model.add_var(var_type=BINARY) for _ in range(U)] for _ in range(B)]

        # elektra per vermogensklasse van busbar naar ac, ieder uur
        ac_from_dc_st = [[[model.add_var(var_type=CONTINUOUS, lb=0,
                           ub=self.battery_options[b]["discharge stages"][ds]["power"]/1000)
                           for _ in range(U)] for ds in range(DS[b])] for b in range(B)]
        ac_from_dc_st_on = [[[model.add_var(var_type=BINARY)
                              for _ in range(U)] for _ in range(DS[b])] for b in range(B)]

        # energiebalans dc
        dc_from_ac = [[model.add_var(var_type=CONTINUOUS, lb=0, ub=max_charge_power[b])
                       for _ in range(U)] for b in range(B)]
        dc_to_ac = [[model.add_var(var_type=CONTINUOUS, lb=0, ub=max_discharge_power[b])
                     for _ in range(U)] for b in range(B)]
        dc_from_bat = [[model.add_var(var_type=CONTINUOUS, lb=0, ub=max_dc_from_bat_power[b])
                        for _ in range(U)] for b in range(B)]
        dc_to_bat = [[model.add_var(var_type=CONTINUOUS, lb=0, ub=max_dc_to_bat_power[b])
                      for _ in range(U)] for b in range(B)]

        # SoC
        soc = [[model.add_var(var_type=CONTINUOUS, lb=min(start_soc[b], float(self.battery_options[b]["lower limit"])),
                ub=max(start_soc[b], float(self.battery_options[b]["upper limit"])))
                for _ in range(U + 1)] for b in range(B)]
        soc_low = [[model.add_var(var_type=CONTINUOUS,
                    lb=min(start_soc[b], float(self.battery_options[b]["lower limit"])),
                    ub=opt_low_level[b]) for _ in range(U + 1)] for b in range(B)]
        soc_mid = [[model.add_var(var_type=CONTINUOUS, lb=0,
                    ub=-opt_low_level[b] + max(start_soc[b], float(self.battery_options[b]["upper limit"])))
                    for _ in range(U + 1)] for b in range(B)]

        # alle constraints
        for b in range(B):
            for u in range(U):
                # laden, alles uitgedrukt in vermogen kW
                # met aan/uit
                '''
                for cs in range(CS[b]):
                    model += (ac_to_dc_st[b][cs][u] <=
                        self.battery_options[b]["charge stages"][cs]["power"] * ac_to_dc_st_on[b][cs][u]/1000)
                for cs in range(CS[b])[1:]:
                    model += (ac_to_dc_st[b][cs][u] >=
                        self.battery_options[b]["charge stages"][cs - 1]["power"] * ac_to_dc_st_on[b][cs][u]/1000)

                model += ac_to_dc[b][u] == xsum(ac_to_dc_st[b][cs][u] for cs in range(CS[b]))
                model += (xsum(ac_to_dc_st_on[b][cs][u] for cs in range(CS[b]))) <= 1
                model += dc_from_ac[b][u] == xsum(ac_to_dc_st[b][cs][u] * \
                                    self.battery_options[b]["charge stages"][cs]["efficiency"] for cs in range(CS[b]))
                '''
                # met sos
                model += xsum(ac_to_dc_w[b][u][cs] for cs in range(CS[b])) == 1
                model += xsum(ac_to_dc_w[b][u][cs] * ac_to_dc_samples[b][cs]
                              for cs in range(CS[b])) == ac_to_dc[b][u]
                model += xsum(ac_to_dc_w[b][u][cs] * dc_from_ac_samples[b][cs]
                              for cs in range(CS[b])) == dc_from_ac[b][u]
                model.add_sos([(ac_to_dc_w[b][u][cs], ac_to_dc_samples[b][cs])
                               for cs in range(CS[b])], 2)
                # tot hier met sos

                # ontladen
                for ds in range(DS[b]):
                    model += ac_from_dc_st[b][ds][u] <= self.battery_options[b]["discharge stages"][ds]["power"] * \
                        ac_from_dc_st_on[b][ds][u]/1000
                for ds in range(DS[b])[1:]:
                    model += ac_from_dc_st[b][ds][u] >= self.battery_options[b]["discharge stages"][ds - 1]["power"] * \
                        ac_from_dc_st_on[b][ds][u]/1000

                model += ac_from_dc[b][u] == xsum(ac_from_dc_st[b][ds][u] for ds in range(DS[b]))
                model += (xsum(ac_from_dc_st_on[b][ds][u] for ds in range(DS[b]))) <= 1
                model += dc_to_ac[b][u] == xsum(ac_from_dc_st[b][ds][u] / self.battery_options[b]
                                                ["discharge stages"][ds]["efficiency"] for ds in range(DS[b]))

        for b in range(B):
            for u in range(U + 1):
                model += soc[b][u] == soc_low[b][u] + soc_mid[b][u]
            model += soc[b][0] == start_soc[b]

            entity_min_soc_end = self.config.get(["entity min soc end opt"], self.battery_options[b], None)
            if entity_min_soc_end is None:
                min_soc_end_opt = 0
            else:
                min_soc_end_opt = float(self.get_state(entity_min_soc_end).state)

            entity_max_soc_end = self.config.get(["entity max soc end opt"], self.battery_options[b], None)
            if entity_max_soc_end is None:
                max_soc_end_opt = 100
            else:
                max_soc_end_opt = float(self.get_state(entity_max_soc_end).state)
            if max_soc_end_opt <= min_soc_end_opt:
                logging.error(f"'max soc end opt' ({max_soc_end_opt}) moet groter zijn dan "
                              f"'min soc end opt' ({min_soc_end_opt}); het programma kan nu geen optimale "
                              f"oplossing berekenem")
                return

            model += soc[b][U] >= max(opt_low_level[b] / 2, min_soc_end_opt)
            model += soc[b][U] <= max_soc_end_opt
            for u in range(U):
                model += (soc[b][u + 1] == soc[b][u] +
                          (dc_to_bat[b][u] * eff_dc_to_bat[b] * hour_fraction[u] / one_soc[b]) -
                          ((dc_from_bat[b][u] * hour_fraction[u] / eff_bat_to_dc[b]) / one_soc[b]))
                model += pv_prod_dc_sum[b][u] == xsum(pv_prod_dc[b][s][u] * pv_dc_on_off[b][s][u]
                                                      for s in range(pv_dc_num[b]))
                # nakijken!!!
                model += (dc_from_ac[b][u] + dc_from_bat[b][u] + pv_prod_dc_sum[b][u] ==
                          dc_to_ac[b][u] + dc_to_bat[b][u])
                model += dc_from_ac[b][u] <= ac_to_dc_on[b][u] * max_charge_power[b]
                model += ac_from_dc[b][u] <= ac_from_dc_on[b][u] * max_discharge_power[b]
                model += (ac_to_dc_on[b][u] + ac_from_dc_on[b][u]) <= 1
            for s in range(pv_dc_num[b]):
                entity_pv_switch = self.config.get(["entity pv switch"], self.battery_options[b]["solar"][s], None)
                if entity_pv_switch == "":
                    entity_pv_switch = None
                if entity_pv_switch is None:
                    for u in range(U):
                        model += pv_dc_on_off[b][s][u] == 1

        #####################################
        #             boiler                #
        #####################################
        boiler_on = [model.add_var(var_type=BINARY) for _ in range(U)]
        self.boiler_present = self.config.get(["boiler present"], self.boiler_options,
                                              "true").lower() == "true"
        if not self.boiler_present:
            # default values
            boiler_setpoint = 50
            boiler_hysterese = 10
            spec_heat_boiler = 200 * 4.2 + 100 * 0.5  # kJ/K
            cop_boiler = 3
            boiler_temp = [model.add_var(var_type=CONTINUOUS, lb=20, ub=20) for _ in range(U + 1)]  # end temp boiler
            c_b = [model.add_var(var_type=CONTINUOUS, lb=0, ub=0) for _ in range(U)]  # consumption boiler
            model += xsum(boiler_on[j] for j in range(U)) == 0
            logging.info(f"Geen boiler aanwezig")
        else:
            # 50 huidige boilertemperatuur ophalen uit ha
            boiler_act_temp = float(self.get_state(self.boiler_options["entity actual temp."]).state)
            boiler_setpoint = float(self.get_state(self.boiler_options["entity setpoint"]).state)
            boiler_hysterese = float(self.get_state(self.boiler_options["entity hysterese"]).state)
            # 0.4 #K/uur instelbaar
            boiler_cooling = self.boiler_options["cooling rate"]
            # 45 # oC instelbaar daaronder kan worden verwarmd
            boiler_bovengrens = self.boiler_options["heating allowed below"]
            boiler_bovengrens = min(boiler_bovengrens, boiler_setpoint)
            # 41 #C instelbaar daaronder moet worden verwarmd
            boiler_ondergrens = boiler_setpoint - boiler_hysterese
            vol = self.boiler_options["volume"]  # liter
            # spec heat in kJ/K = vol in liter * 4,2 J/liter + 100 kg * 0,5 J/kg
            spec_heat_boiler = vol * 4.2 + 200 * 0.5  # kJ/K
            cop_boiler = self.boiler_options["cop"]
            power = self.boiler_options["elec. power"]  # W

            # tijdstip index waarop boiler kan worden verwarmd
            boiler_start = int(max(0, min(23, int((boiler_act_temp - boiler_bovengrens) / boiler_cooling))))

            # tijdstip index waarop boiler nog aan kan
            # (41-40)/0.4=2.5
            boiler_end = int(min(U - 1, max(0, int((boiler_act_temp - boiler_ondergrens) / boiler_cooling))))
            boiler_temp = [model.add_var(var_type=CONTINUOUS,
                                         lb=min(boiler_act_temp, boiler_setpoint - boiler_hysterese - 10),
                                         ub=boiler_setpoint + 10)
                           for _ in range(U + 1)]  # end temp boiler

            if boiler_start > boiler_end:  # geen boiler opwarming in deze periode
                c_b = [model.add_var(var_type=CONTINUOUS, lb=0, ub=0)
                       for _ in range(U)]  # consumption boiler
                model += xsum(boiler_on[j] for j in range(U)
                              [boiler_start:boiler_end + 1]) == 0
                logging.debug(f"Boiler: geen opwarming")
                boiler_end_temp = boiler_act_temp - boiler_cooling * U
                logging.debug(f"Boiler eind temperatuur: {boiler_end_temp}")
                for u in range(U):
                    # opwarming in K = kWh opwarming * 3600 = kJ / spec heat boiler - 3
                    model += boiler_temp[u + 1] == boiler_temp[u] - boiler_cooling
            else:
                logging.info(f"Boiler opwarmen wordt ingepland tussen: {uur[boiler_start]} en {uur[boiler_end]} uur")
                needed_elec = [0.0 for _ in range(U)]
                needed_time = [0 for _ in range(U)]
                needed_heat = max(0.0, float(spec_heat_boiler * (
                    boiler_setpoint - (boiler_act_temp - 4 - boiler_cooling * (boiler_end - boiler_start))) / 3600))
                for u in range(boiler_start, boiler_end + 1):
                    needed_elec[u] = needed_heat / cop_boiler  # kWh
                    needed_time[u] = needed_elec[u] * 1000 / power  # hour

                c_b = [model.add_var(var_type=CONTINUOUS, lb=0, ub=needed_elec[u])
                       for u in range(U)]  # cons. boiler
                for u in range(U):
                    model += c_b[u] == boiler_on[u] * needed_elec[u]
                    if u < boiler_start:
                        model += boiler_on[u] == 0
                    elif u > boiler_end:
                        model += boiler_on[u] == 0
                model += xsum(boiler_on[j] for j in range(U)
                              [boiler_start:boiler_end + 1]) == 1
                model += boiler_temp[0] == boiler_act_temp
                for u in range(U):
                    # opwarming in K = kWh opwarming * 3600 = kJ / spec heat boiler - 3
                    model += boiler_temp[u + 1] == boiler_temp[u] - boiler_cooling + c_b[u] * cop_boiler \
                        * 3600 / spec_heat_boiler

        ################################################
        #             electric vehicles
        ################################################
        EV = len(self.ev_options)
        actual_soc = []
        wished_level = []
        level_margin = []
        ready_u = []
        hours_needed = []
        max_power = []
        energy_needed = []
        ev_plugged_in = []
        ev_position = []
        #  now_dt = dt.datetime.now()
        charge_stages = []
        ampere_factor = []
        ECS = []
        for e in range(EV):
            ev_capacity = self.ev_options[e]["capacity"]
            # plugged = self.get_state(self.ev_options["entity plugged in"]).state
            try:
                plugged_in = self.get_state(self.ev_options[e]["entity plugged in"]).state == "on"
            except Exception as ex:
                logging.error(ex)
                plugged_in = False
            ev_plugged_in.append(plugged_in)
            try:
                position = self.get_state(self.ev_options[e]["entity position"]).state
            except Exception as ex:
                logging.error(ex)
                position = "away"
            ev_position.append(position)
            try:
                soc_state = float(self.get_state(self.ev_options[e]["entity actual level"]).state)
            except Exception as ex:
                logging.error(ex)
                soc_state = 100.0

            # onderstaande regel evt voor testen
            # soc_state = min(soc_state, 90.0)

            actual_soc.append(soc_state)
            wished_level.append(float(self.get_state(self.ev_options[e]["charge scheduler"]["entity set level"]).state))
            level_margin.append(self.config.get(["level margin"], self.ev_options[e]["charge scheduler"], 0))
            ready_str = self.get_state(self.ev_options[e]["charge scheduler"]["entity ready datetime"]).state
            if len(ready_str) > 9:
                # dus met datum en tijd
                ready = dt.datetime.strptime(ready_str, '%Y-%m-%d %H:%M:%S')
            else:
                ready = dt.datetime.strptime(ready_str, '%H:%M:%S')
                ready = dt.datetime(start_dt.year, start_dt.month, start_dt.day, ready.hour, ready.minute)
                if (ready.hour == start_dt.hour and ready.minute < start_dt.minute) or (ready.hour < start_dt.hour):
                    ready = ready + dt.timedelta(days=1)
            hours_available = (ready - start_dt).total_seconds()/3600
            ev_stages = self.ev_options[e]["charge stages"]
            if ev_stages[0]["ampere"] != 0.0:
                ev_stages = [{"ampere": 0.0, "efficiency": 1}] + ev_stages
            charge_stages.append(ev_stages)
            ECS.append(len(charge_stages[e]))
            max_ampere = charge_stages[e][-1]["ampere"]
            try:
                max_ampere = float(max_ampere)
            except ValueError:
                max_ampere = 10
            charge_three_phase = self.config.get(["charge three phase"], self.ev_options[e], "true").lower() == "true"
            if charge_three_phase:
                ampere_f = 3
            else:
                ampere_f = 1
            ampere_factor.append(ampere_f)
            max_power.append(max_ampere * ampere_f * 230 / 1000)  # vermogen in kW
            logging.info(f"Instellingen voor laden van EV: {self.ev_options[e]['name']}")

            logging.info(f" Ampere  Effic. Grid kW Accu kW")
            for cs in range(ECS[e]):
                if not ("efficiency" in charge_stages[e][cs]):
                    charge_stages[e][cs]["efficiency"] = 1.0
                charge_stages[e][cs]["power"] = charge_stages[e][cs]["ampere"] * 230 * ampere_factor[e]/1000
                charge_stages[e][cs]["accu_power"] = charge_stages[e][cs]["power"] * charge_stages[e][cs]["efficiency"]
                logging.info(f"{charge_stages[e][cs]['ampere']:>7.2f} {charge_stages[e][cs]['efficiency']:>7.2f} "
                             f"{charge_stages[e][cs]['power']:>7.2f} {charge_stages[e][cs]['accu_power']:>7.2f}")

            '''
            #test voor bug
            ev_plugged_in.append(True)
            wished_level.append(float(self.get_state(self.ev_options[e]["charge scheduler"]["entity set level"]).state))
            ev_position.append("home")
            actual_soc.append(40)
            max_power.append(10 * 230 / 1000)
            #tot hier
            '''
            logging.info(f"Capaciteit accu: {ev_capacity} kWh")
            logging.info(f"Maximaal laadvermogen: {max_power[e]} kW")
            logging.info(f"Klaar met laden op: {ready.strftime('%d-%m-%Y %H:%M:%S')}")
            logging.info(f"Huidig laadniveau: {actual_soc[e]} %")
            logging.info(f"Gewenst laadniveau:{wished_level[e]} %")
            logging.info(f"Marge voor het laden: {level_margin[e]} %")
            logging.info(f"Locatie: {ev_position[e]}")
            logging.info(f"Ingeplugged:{ev_plugged_in[e]}")
            e_needed = ev_capacity * (wished_level[e] - actual_soc[e]) / 100
            e_needed = min(e_needed, max_power[e] * hours_available * charge_stages[e][-1]["efficiency"])
            energy_needed.append(e_needed)  # in kWh
            logging.info(f"Benodigde energie: {energy_needed[e]} kWh")
            # uitgedrukt in aantal uren; bijvoorbeeld 1,5
            time_needed = energy_needed[e] / (max_power[e] * charge_stages[e][-1]["efficiency"])
            logging.info(f"Tijd nodig om te laden: {time_needed} uur")
            old_switch_state = self.get_state(self.ev_options[e]["charge switch"]).state
            old_ampere_state = self.get_state(self.ev_options[e]["entity set charging ampere"]).state
            # afgerond naar boven in hele uren
            hours_needed.append(math.ceil(time_needed))
            logging.info(f"Afgerond naar hele uren: {hours_needed[e]}")
            logging.info(f"Stand laden schakelaar: {old_switch_state}")
            logging.info(f"Stand aantal ampere laden: {old_ampere_state} A")
            ready_index = U
            reden = ""
            if (wished_level[e] - level_margin[e]) <= actual_soc[e]:
                reden = (f" werkelijk niveau ({actual_soc[e]:.1f}%) hoger is of gelijk aan gewenst niveau "
                         f"({wished_level[e]:.1f}% minus de marge {level_margin[e]}%),")
            if not (ev_position[e] == "home"):
                reden = reden + " auto is niet huis,"
            if not ev_plugged_in[e]:
                reden = reden + " auto is niet ingeplugd,"
            if not (tijd[0] < ready):
                reden = reden + f" opgegeven tijdstip ({str(ready)}) is verouderd,"
            if tijd[U-1] < ready:
                reden = reden + f" opgegeven tijdstip ({str(ready)}) ligt voorbij de planningshorizon ({tijd[U - 1]}),"
            if (ev_plugged_in[e] and (ev_position[e] == "home") and
                    (wished_level[e] - level_margin[e] > actual_soc[e]) and (tijd[0] < ready)):
                for u in range(U):
                    if (tijd[u] + dt.timedelta(hours=1)) >= ready:
                        ready_index = u
                        break
            if ready_index == U:
                if len(reden) > 0:
                    reden = reden[:-1] + "."
                logging.info(f"Opladen wordt niet ingepland, omdat{reden}")
            else:
                logging.info(f"Opladen wordt ingepland.")
            ready_u.append(ready_index)

        # charger_on = [[model.add_var(var_type=BINARY) for u in range(U)] for e in range(EV)]
        # charger_ampere = [[model.add_var(var_type=CONTINUOUS, lb=0, ub= charge_stages[e][-1]["ampere"])
        #                     for cs in range(ECS[e])] for e in range(EV)]
        charger_power = [[[model.add_var(var_type=CONTINUOUS, lb=0, ub=max_power[e])
                           for _ in range(U)] for _ in range(ECS[e])] for e in range(EV)]
        charger_factor = [[[model.add_var(var_type=CONTINUOUS, lb=0, ub=1) for _ in range(U)]
                           for _ in range(ECS[e])] for e in range(EV)]
        charger_on = [[[model.add_var(var_type=BINARY) for _ in range(U)]
                       for _ in range(ECS[e])] for e in range(EV)]

        c_ev = [[model.add_var(var_type=CONTINUOUS, lb=0, ub=max_power[e])
                 for _ in range(U)] for e in range(EV)]  # consumption charger
        ev_accu_in = [[model.add_var(var_type=CONTINUOUS, lb=0, ub=max_power[e])
                       for _ in range(U)] for e in range(EV)]  # load battery

        for e in range(EV):
            if (energy_needed[e] > 0) and (ready_u[e] < U):
                for u in range(ready_u[e] + 1):
                    # laden, alles uitgedrukt in vermogen kW
                    for cs in range(ECS[e]):
                        # daadwerkelijk ac vermogen = vermogen van de stap x oplaadfactor (0..1)
                        model += charger_power[e][cs][u] == charge_stages[e][cs]["power"] * charger_factor[e][cs][u]
                        # idem met schakelaar
                        model += charger_power[e][cs][u] <= max_power[e] * charger_on[e][cs][u]
                    # som van alle oplaadfactoren is 1
                    model += (xsum(charger_factor[e][cs][u] for cs in range(ECS[e]))) == 1
                    # som van alle schakelaars boven 0 A en kleiner of gelijk aan 1
                    model += (xsum(charger_on[e][cs][u] for cs in range(ECS[e])[1:])) <= 1
                    model += c_ev[e][u] == xsum(charger_power[e][cs][u] * hour_fraction[u] for cs in range(ECS[e]))
                    model += ev_accu_in[e][u] == xsum(charge_stages[e][cs]["accu_power"] * hour_fraction[u] *
                                                      charger_factor[e][cs][u] for cs in range(ECS[e]))
                model += energy_needed[e] == xsum(ev_accu_in[e][u] for u in range(ready_u[e] + 1))
                for u in range(U)[ready_u[e]+1:]:
                    model += c_ev[e][u] == 0

                '''
                max_beschikbaar = 0
                for u in range(ready_u[e] + 1):
                    model += c_ev[e][u] <= charger_on[e][u] * hour_fraction[u] * max_power[e]
                    max_beschikbaar += hour_fraction[u] * max_power[e]
                for u in range(ready_u[e] + 1, U):
                    model += charger_on[e][u] == 0
                    model += c_ev[e][u] == 0
                model += xsum(charger_on[e][j] for j in range(ready_u[e] + 1)) == hours_needed[e]
                model += xsum(c_ev[e][u] for u in range(ready_u[e] + 1)) == min(max_beschikbaar, energy_needed[e])
                '''
            else:
                model += xsum(c_ev[e][u] for u in range(U)) == 0
                for u in range(U):
                    model += c_ev[e][u] == 0

        ##################################################################
        #            salderen                                            #
        ##################################################################
        # total consumption per hour: base_load plus accuload
        # inkoop + pv + accu_out = teruglevering + base_cons + accu_in + boiler + ev + ruimteverwarming
        # in code:  c_l + pv + accu_out = c_t + b_l + accu_in + hw + ev + rv
        # c_l : verbruik levering
        # c_t : verbruik teruglevering met saldering
        # c_t_notax : verbruik teruglevering zonder saldering
        # pv : opwekking zonnepanelen

        # anders geschreven c_l = c_t + ct_notax + b_l + accu_in + hw + rv - pv - accu_out
        # continue variabele c consumption in kWh/h
        # minimaal 20 kW terugleveren max 20 kW leveren (3 x 25A = 17,5 kW)
        # instelbaar maken?
        # levering
        c_l = [model.add_var(var_type=CONTINUOUS, lb=0, ub=self.grid_max_power) for _ in range(U)]
        # teruglevering
        c_t_total = [model.add_var(var_type=CONTINUOUS, lb=0, ub=self.grid_max_power) for _ in range(U)]
        c_t_w_tax = [model.add_var(var_type=CONTINUOUS, lb=0, ub=self.grid_max_power) for _ in range(U)]
        c_l_on = [model.add_var(var_type=BINARY) for _ in range(U)]
        c_t_on = [model.add_var(var_type=BINARY) for _ in range(U)]

        # salderen == True
        # c_t_no_tax = [model.add_var(var_type=CONTINUOUS, lb=0, ub=0) for u in range(U)]

        if salderen:
            c_t_no_tax = [model.add_var(var_type=CONTINUOUS, lb=0, ub=0) for _ in range(U)]
        else:
            # alles wat meer wordt teruggeleverd dan geleverd (c_t_no_tax) wordt niet gesaldeerd
            # (geen belasting terug): tarief pt_notax
            c_t_no_tax = [model.add_var(var_type=CONTINUOUS, lb=0, ub=self.grid_max_power) for _ in range(U)]
            model += (xsum(c_t_w_tax[u] for u in range(U)) + production_today) <= \
                     (xsum(c_l[u] for u in range(U)) + consumption_today)
        # netto per uur alleen leveren of terugleveren niet tegelijk?
        for u in range(U):
            model += c_t_total[u] == c_t_w_tax[u] + c_t_no_tax[u]
            model += c_l[u] <= c_l_on[u] * 20
            model += c_t_total[u] <= c_t_on[u] * 20
            model += c_l_on[u] + c_t_on[u] <= 1

        #####################################
        #              heatpump             #
        #####################################

        self.heater_present = self.heating_options["heater present"].lower() == "true"
        if not self.heater_present:
            c_hp = [model.add_var(var_type=CONTINUOUS, lb=0, ub=0)
                    for _ in range(U)]  # elektriciteitsverbruik in kWh/h
            p_hp = None
            h_hp = None
        else:
            degree_days = self.meteo.calc_graaddagen()
            if U > 24:
                degree_days += self.meteo.calc_graaddagen(
                    date=dt.datetime.combine(
                    dt.date.today() + dt.timedelta(days=1), dt.datetime.min.time()))
            logging.info(f"Warmtepomp")
            logging.info(f"Graaddagen: {degree_days:.1f}")  # 3.6  heat factor kWh th / K.day
            degree_days_factor = self.heating_options["degree days factor"]
            heat_produced = float(self.get_state("sensor.daily_heat_production_heating").state)
            heat_needed = max(0.0, degree_days * degree_days_factor - heat_produced)  # heet needed
            stages = self.heating_options["stages"]
            S = len(stages)
            c_hp = [model.add_var(var_type=CONTINUOUS, lb=0, ub=10)
                    for _ in range(U)]  # elektriciteitsverbruik in kWh/h
            # p_hp[s][u]: het gevraagde vermogen in W in dat uur
            p_hp = [[model.add_var(var_type=CONTINUOUS, lb=0, ub=stages[s]["max_power"])
                     for _ in range(U)] for s in range(S)]

            # schijven aan/uit, iedere schijf kan maar een keer in een uur
            hp_on = [[model.add_var(var_type=BINARY) for _ in range(U)] for _ in range(S)]

            # verbruik per uur
            for u in range(U):
                # verbruik in kWh is totaal vermogen in W/1000
                model += c_hp[u] == (xsum(p_hp[s][u] for s in range(S))) / 1000
                # kosten
                # model += k_hp[u] == c_hp[u] * pl[u]  # kosten = verbruik x tarief

            # geproduceerde warmte kWh per uur
            h_hp = [model.add_var(var_type=CONTINUOUS, lb=0, ub=10000) for _ in range(U)]

            # beschikbaar vermogen x aan/uit, want p_hpx[u] X hpx_on[u] kan niet
            for u in range(U):
                for s in range(S):
                    model += p_hp[s][u] <= stages[s]["max_power"] * hp_on[s][u]
                # ieder uur maar een aan
                model += (xsum(hp_on[s][u] for s in range(S))) + boiler_on[u] == 1
                # geproduceerde warmte = vermogen in W * COP_schijf /1000 in kWh
                model += h_hp[u] == xsum((p_hp[s][u] * stages[s]["cop"]/1000) for s in range(S)) * hour_fraction[u]
            # som van alle geproduceerde warmte == benodigde warmte
            model += xsum(h_hp[u] for u in range(U)) == heat_needed

        ########################################################################
        # apparaten /machines
        ########################################################################
        program_selected = []  # "kleur 30", "eco"]
        M = len(self.machines)
        R = []  # aantal mogelijke runs
        RL = []  # lengte van een run
        KW = []  # aantal kwartieren
        ma_uur_kw = []  # per machine een list met beschikbare kwartieren
        ma_kw_dt = []   # per machine een list op welk tijdstip een kwartier begint
        program_index = []
        ma_name = []
        ma_entity_plan_start = []
        ma_entity_plan_end = []
        for m in range(M):
            error = False
            ma_name.append(self.machines[m]['name'])
            # machines[m]["power"] = [0] + machines[m]["power"] + [0]
            start_entity = self.config.get(["entity start window"], self.machines[m], None)
            start_ma_dt = start_dt
            ready_ma_dt = uur[U-1]
            if start_entity is None:
                logging.error(f"De 'entity start window' is niet gedefinieerd bij de instellingen van {ma_name[m]}.")
                logging.error(f"Apparaat {ma_name[m]} wordt niet ingepland.")
                error = True
            else:
                start = self.get_state(start_entity).state
                start_ma_dt = convert_timestr(start, start_dt)
            ready_entity = self.config.get(["entity end window"], self.machines[m], None)
            if ready_entity is None:
                logging.error(f"De 'entity end window' is niet gedefinieerd bij de instellingen van {ma_name[m]}.")
                if not error:
                    logging.error(f"Apparaat {ma_name[m]} wordt niet ingepland.")
                    error = True
            else:
                ready = self.get_state(ready_entity).state
                ready_ma_dt = convert_timestr(ready, start_ma_dt)
            if not error and start_ma_dt > ready_ma_dt:
                if ready_ma_dt > start_ma_dt:
                    logging.info(f"Apparaat {ma_name[m]} wordt nog niet ingepland: de planningsperiode is begonnen")
                    error = True
                else:
                    ready_ma_dt = ready_ma_dt + dt.timedelta(days=1)
            if ready_ma_dt > tijd[U-1]:
                logging.info(f"Machine {ma_name[m]} wordt niet ingepland, want {ready_ma_dt} "
                             f"ligt voorbij de planningshorizon {uur[U-1]}")
                error = True
            if error:
                kw_num = 0
            else:
                delta = ready_ma_dt - start_ma_dt
                kw_num = math.ceil(delta.seconds / 900)
            KW.append(kw_num)
            entity_machine_program = self.config.get(["entity selected program"], self.machines[m], None)
            if entity_machine_program:
                try:
                    program_selected.append(self.get_state(entity_machine_program).state)
                except Exception as ex:
                    logging.error(ex)
            p = next((i for i, item in enumerate(self.machines[m]["programs"]) if item["name"] ==
                      program_selected[m]), 0)
            program_index.append(p)
            RL.append(len(self.machines[m]["programs"][p]["power"]))  # aantal stages
            if RL[m] == 0:
                logging.info(f"machine {ma_name[m]} wordt niet ingepland, "
                             f"want er is gekozen voor {program_selected[m]}")
            else:
                logging.info(f"Apparaat {ma_name[m]} met programma '{program_selected[m]}' wordt ingepland tussen "
                             f"{start_ma_dt.strftime('%Y-%m-%d %H:%M')} en {ready_ma_dt.strftime('%Y-%m-%d %H:%M')}.")

            uur_kw = []
            kw_dt = []
            kwartier_dt = start_ma_dt
            for u in range(U):
                uur_kw.append([])
            for kw in range(kw_num):
                uur_index = calc_uur_index(kwartier_dt, tijd)
                if uur_index < U:
                    uur_kw[uur_index].append(kw)
                kw_dt.append(kwartier_dt)
                kwartier_dt = kwartier_dt + dt.timedelta(seconds=900)
            ma_uur_kw.append(uur_kw)
            ma_kw_dt.append(kw_dt)
            R.append(min(KW[m], KW[m] - RL[m] + 1))  # aantal runs = aantal kwartieren - aantal stages + 1
            ma_entity_plan_start.append(self.config.get(["entity calculated start"], self.machines[m], None))
            ma_entity_plan_end.append(self.config.get(["entity calculated end"], self.machines[m], None))
        # ma_start : wanneer machine start = 1 anders = 0
        ma_start = [[model.add_var(var_type=BINARY) for _ in range(KW[m])] for m in range(M)]

        # machine aan per kwartier per run
        # ma_on = [[[model.add_var(var_type=BINARY) for kw in range(KW[m])] for r in range(R[m])] for m in range(M)]

        # consumption per kwartier
        c_ma_kw = [[model.add_var(var_type=CONTINUOUS, lb=0,
                                  ub=math.ceil(max(self.machines[m]["programs"][program_index[m]]["power"], default=0)))
                    for _ in range(KW[m])] for m in range(M)]

        c_ma_u = [[model.add_var(var_type=CONTINUOUS, lb=0) for _ in range(U)] for _ in range(M)]

        # kosten per uur
        # k_ma = [[model.add_var(var_type=CONTINUOUS) for _ in range(U)] for _ in range(M)]
        # total_cost_ma = [model.add_var(var_type=CONTINUOUS) for _ in range(M)]
        # total_cost = model.add_var(var_type=CONTINUOUS)

        #  constraints
        for m in range(M):
            # maar 1 start
            if KW[m] == 0:
                model += xsum(ma_start[m][kw] for kw in range(KW[m])) == 0
            else:
                model += xsum(ma_start[m][kw] for kw in range(KW[m])) == 1

            # kan niet starten als je de run niet kan afmaken
            for kw in range(KW[m])[KW[m]-RL[m]:]:
                model += ma_start[m][kw] == 0

            if self.log_level == logging.DEBUG:
                logging.debug(f"Per kwartier welke run en met welk vermogen")
                for kw in range(KW[m]):
                    print(f"kw: {kw} tijd: {ma_kw_dt[m][kw].strftime('%H:%M')} "
                          f"range r: {max(0, kw - RL[m]+1)} <-> {min(kw, R[m])+1} r:", end=" ")
                    for r in range(R[m])[max(0, kw - RL[m]+1): min(kw, R[m])+1]:
                        print(f"{r} power: {self.machines[m]['programs'][program_index[m]]['power'][kw-r]}", end=" ")
                    print()

            for kw in range(KW[m]):
                model += c_ma_kw[m][kw] == xsum(self.machines[m]["programs"][program_index[m]]["power"][kw-r] *
                                                ma_start[m][r]/4000
                                                for r in range(R[m])[max(0, kw - RL[m]+1): min(kw, R[m])+1])
            for u in range(U):
                if len(ma_uur_kw[m][u]) == 0:
                    model += c_ma_u[m][u] == 0
                else:
                    model += c_ma_u[m][u] == xsum(c_ma_kw[m][kw] for kw in ma_uur_kw[m][u])

        #####################################################
        # alle verbruiken in de totaal balans in kWh
        #####################################################
        for u in range(U):
            model += (c_l[u] == c_t_total[u] + b_l[u] * hour_fraction[u] +
                      xsum(ac_to_dc[b][u] - ac_from_dc[b][u] for b in range(B)) * hour_fraction[u] +
                      # xsum(ac_to_dc[b][u] - ac_from_dc[b][u] for b in range(B)) +
                      c_b[u] + xsum(c_ev[e][u] for e in range(EV)) +
                      c_hp[u] * hour_fraction[u] + xsum(c_ma_u[m][u] for m in range(M)) -
                      xsum(pv_ac[s][u] for s in range(solar_num)))

        # cost variabele
        cost = model.add_var(var_type=CONTINUOUS, lb=-1000, ub=1000)
        delivery = model.add_var(var_type=CONTINUOUS, lb=0, ub=1000)
        model += delivery == xsum(c_l[u] for u in range(U))

        if salderen:
            p_bat = p_avg
        else:
            p_bat = sum(pt_notax)/U

        # alles in kWh * prijs = kosten in euro
        model += cost == xsum(c_l[u] * pl[u] - c_t_w_tax[u] * pt[u] - c_t_no_tax[u] * pt_notax[u] for u in range(U)) + \
            xsum(xsum((dc_to_bat[b][u] + dc_from_bat[b][u]) * kwh_cycle_cost[b] +
                      (opt_low_level[b] - soc_low[b][u]) * 0.0025 for u in range(U)) for b in range(B)) + \
            xsum((soc_mid[b][0] - soc_mid[b][U]) * one_soc[b] * eff_bat_to_dc[b]
                 * avg_eff_dc_to_ac[b] * p_bat for b in range(B))  # waarde opslag accu
        # +(boiler_temp[U] - boiler_ondergrens) * (spec_heat_boiler/(3600 * cop_boiler)) * p_avg # waarde energie boiler

        #####################################################
        #        strategy optimization
        #####################################################
        # settings
        model.max_gap = 0.1
        model.max_nodes = 1500
        if self.log_level > logging.DEBUG:
            model.verbose = 0
        model.check_optimization_results()

        # kosten optimalisering
        strategy = self.strategy.lower()
        if strategy == "minimize cost":
            strategie = 'minimale kosten'
            model.objective = minimize(cost)
            model.optimize()
            if model.num_solutions == 0:
                logging.warning(f"Geen oplossing  voor: {strategy}")
                return
        elif strategy == "minimize consumption":
            strategie = 'minimale levering'
            model.objective = minimize(delivery)
            model.optimize()
            if model.num_solutions == 0:
                logging.warning(f"Geen oplossing  voor: {strategy}")
                return
            min_delivery = max(0.0, delivery.x)
            logging.info("Ronde 1")
            logging.info(f"Kosten (euro): {cost.x:<6.2f}%")
            logging.info(f"Levering (kWh): {delivery.x:<6.2f}%")
            model += (delivery <= min_delivery)
            model.objective = minimize(cost)
            model.optimize()
            if model.num_solutions == 0:
                logging.warning(f"Geen oplossing in na herberekening voor: {strategy}")
                return
            logging.info("Herberekening")
            logging.info(f"Kosten (euro): {cost.x:<6.2f}%")
            logging.info(f"Levering (kWh): {delivery.x:<6.2f}%")
        else:
            logging.error("Kies een strategie in options")
            # strategie = 'niet gekozen'
            return
        logging.info(f"Strategie: {strategie}")

        # Suppress FutureWarning messages
        import warnings
        warnings.simplefilter(action='ignore', category=FutureWarning)

        if model.num_solutions == 0:
            logging.error(f"Er is helaas geen oplossing gevonden, kijk naar je instellingen.")
            return

        # er is een oplossing
        # afdrukken van de resultaten
        logging.info("Het programma heeft een optimale oplossing gevonden.")
        old_cost_gc = 0
        old_cost_da = 0
        sum_old_cons = 0
        org_l = []
        org_t = []
        c_ev_sum = []
        c_ma_sum = []
        accu_in_sum = []
        accu_out_sum = []
        for u in range(U):
            ac_to_dc_sum = 0
            dc_to_ac_sum = 0
            for b in range(B):
                ac_to_dc_sum += ac_to_dc[b][u].x  # / eff_ac_to_dc[b]
                dc_to_ac_sum += ac_from_dc[b][u].x  # * eff_dc_to_ac[b]
            accu_in_sum.append(ac_to_dc_sum)
            accu_out_sum.append(dc_to_ac_sum)
        for u in range(U):
            ev_sum = 0
            for e in range(EV):
                ev_sum += c_ev[e][u].x
            c_ev_sum.append(ev_sum)
            ma_sum = 0
            for m in range(M):
                ma_sum += c_ma_u[m][u].x
            c_ma_sum.append(ma_sum)
        pv_ac_hour_sum = []  # totale bruto pv_dc->ac productie
        solar_hour_sum_org = []  # totale netto pv_ac productie origineel
        solar_hour_sum_opt = []  # totale netto pv_ac productie na optimalisatie
        for u in range(U):
            pv_ac_hour_sum.append(0)
            solar_hour_sum_org.append(0)
            solar_hour_sum_opt.append(0)
            for b in range(B):
                for s in range(pv_dc_num[b]):
                    pv_ac_hour_sum[u] += pv_prod_ac[b][s][u]
            for s in range(solar_num):
                solar_hour_sum_org[u] += solar_prod[s][u]  # pv_ac[s][u].x
                solar_hour_sum_opt[u] += pv_ac[s][u].x
            netto = b_l[u] + c_b[u].x + c_hp[u].x + c_ev_sum[u] + c_ma_sum[u] \
                - solar_hour_sum_org[u] - pv_ac_hour_sum[u]
            sum_old_cons += netto
            if netto >= 0:
                old_cost_gc += netto * p_grl[u]
                old_cost_da += netto * pl[u]
                org_l.append(netto)
                org_t.append(0)
            else:
                old_cost_gc += netto * p_grt[u]
                old_cost_da += netto * pt[u]
                org_l.append(0)
                org_t.append(netto)
        if (not salderen) and (sum_old_cons < 0):
            # er wordt (een deel) niet gesaldeerd
            dag_str = dt.datetime.now().strftime("%Y-%m-%d")
            taxes_l = get_value_from_dict(dag_str, taxes_l_def)
            btw = get_value_from_dict(dag_str, btw_def)
            saldeer_corr_gc = -sum_old_cons * \
                (sum(p_grt) / len(p_grt) - 0.11)
            saldeer_corr_da = -sum_old_cons * taxes_l * (1 + btw)
            old_cost_gc += saldeer_corr_gc
            old_cost_da += saldeer_corr_da
            logging.info(f"Saldeercorrectie: {sum_old_cons:<6.2f} kWh")
            logging.info(f"Saldeercorrectie niet geoptimaliseerd reg. tarieven: {saldeer_corr_gc:<6.2f} euro")
            logging.info(f"Saldeercorrectie niet geoptimaliseerd day ahead tarieven: {saldeer_corr_da:<6.2f} euro")
        else:
            logging.info(f"Geen saldeer correctie")
        logging.info(f"Niet geoptimaliseerd, kosten met reguliere tarieven: {old_cost_gc:<6.2f}")
        logging.info(f"Niet geoptimaliseerd, kosten met day ahead tarieven: {old_cost_da:<6.2f}")
        logging.info(f"Geoptimaliseerd, kosten met day ahead tarieven: {cost.x:<6.2f}")
        logging.info(f"Levering: {delivery.x:<6.2f} (kWh)")
        if self.boiler_present:
            boiler_at_23 = ((boiler_temp[U].x - (boiler_setpoint - boiler_hysterese)) *
                            (spec_heat_boiler / (3600 * cop_boiler)))
            logging.info(f"Waarde boiler om 23 uur: {boiler_at_23:<0.2f} kWh")
        if self.heater_present:
            logging.info("\nInzet warmtepomp")
            # df_hp = pd.DataFrame(columns=["u", "tar", "p0", "p1", "p2", p3     p4     p5     p6     p7
            # heat   cons"])
            logging.info(f"u     tar     p0     p1     p2     p3     p4     p5     p6     p7   heat   cons")
            for u in range(U):
                logging.info(f"{uur[u]:2.0f} {pl[u]:6.4f} {p_hp[0][u].x:6.0f} {p_hp[1][u].x:6.0f} "
                             f"{p_hp[2][u].x:6.0f} {p_hp[3][u].x:6.0f} {p_hp[4][u].x:6.0f} "
                             f"{p_hp[5][u].x:6.0f} {p_hp[6][u].x:6.0f} {p_hp[7][u].x:6.0f} "
                             f"{h_hp[u].x:6.2f} {c_hp[u].x:6.2f}")

        # overzicht per ac-accu:
        pd.options.display.float_format = '{:6.2f}'.format
        df_accu = []
        df_soc = pd.DataFrame(columns=["tijd", "soc"])
        df_soc.index = pd.to_datetime(df_soc["tijd"])
        tijd_soc = tijd.copy()
        tijd_soc.append(tijd_soc[U - 1] + datetime.timedelta(hours=1))
        for b in range(B):
            df_soc["soc_"+str(b)] = None
            cols = [['uur', 'ac->', 'eff', '->dc', 'pv->dc', 'dc->', 'eff', '->bat', 'o_eff', 'SoC'],
                    ["", "kWh", "%", "kWh", "kWh", "kWh", "%", "kWh", "%", "%"]]
            df_accu.append(pd.DataFrame(columns=cols))
            for u in range(U):
                """
                for cs in range(CS[b]):
                    if ac_to_dc_st_on[b][cs][u].x == 1:
                        c_stage = cs
                        ac_to_dc_eff = self.battery_options[b]["charge stages"][cs]["efficiency"] * 100.0
                """
                ac_to_dc_netto = (ac_to_dc[b][u].x - ac_from_dc[b][u].x) * hour_fraction[u]
                dc_from_ac_netto = (dc_from_ac[b][u].x - dc_to_ac[b][u].x) * hour_fraction[u]
                if ac_to_dc_netto > 0:
                    ac_to_dc_eff = dc_from_ac_netto * 100.0 / ac_to_dc_netto
                elif ac_to_dc_netto < 0:
                    ac_to_dc_eff = ac_to_dc_netto * 100.0 / dc_from_ac_netto
                else:
                    ac_to_dc_eff = "--"

                dc_to_bat_netto = (dc_to_bat[b][u].x - dc_from_bat[b][u].x) * hour_fraction[u]
                bat_from_dc_netto = ((dc_to_bat[b][u].x * eff_dc_to_bat[b] - dc_from_bat[b][u].x / eff_bat_to_dc[b])
                                     * hour_fraction[u])
                if dc_to_bat_netto > 0:
                    dc_to_bat_eff = bat_from_dc_netto * 100.0/dc_to_bat_netto
                elif dc_to_bat_netto < 0:
                    dc_to_bat_eff = dc_to_bat_netto * 100.0 / bat_from_dc_netto
                else:
                    dc_to_bat_eff = "--"

                if ac_to_dc_netto > 0:
                    overall_eff = bat_from_dc_netto * 100.0 / ac_to_dc_netto
                elif bat_from_dc_netto < 0:
                    overall_eff = ac_to_dc_netto * 100.0 / bat_from_dc_netto
                else:
                    overall_eff = "--"

                """
                for ds in range(DS[b]):
                    if ac_from_dc_st_on[b][ds][u].x == 1:
                        d_stage = ds
                        dc_to_ac_eff = self.battery_options[b]["discharge stages"][ds]["efficiency"] * 100.0
                """

                pv_prod = 0
                for s in range(pv_dc_num[b]):
                    pv_prod += pv_dc_on_off[b][s][u].x * pv_prod_dc[b][s][u] * hour_fraction[u]
                row = [str(uur[u]), ac_to_dc_netto, ac_to_dc_eff, dc_from_ac_netto,  pv_prod,
                       dc_to_bat_netto, dc_to_bat_eff, bat_from_dc_netto, overall_eff, soc[b][u + 1].x]
                df_accu[b].loc[df_accu[b].shape[0]] = row

            # df_accu[b].loc['total'] = df_accu[b].select_dtypes(numpy.number).sum()
            # df_accu[b] = df_accu[b].astype({"uur": int})
            # df_accu[b].set_index(["uur"])
            # df_accu[b][~df_accu[b].index.duplicated()]
            try:
                df_accu[b].loc["Total"] = df_accu[b].sum(axis=0, numeric_only=True)
                totals = True
            except Exception as ex:
                logging.info(ex)
                logging.info(f"Totals of accu {self.battery_options[b]['name']} cannot be calculated")
                totals = False

            if totals:
                df_accu[b].at[df_accu[b].index[-1], "uur"] = "Totaal"
                df_accu[b].at[df_accu[b].index[-1], "eff"] = "--"
                df_accu[b].at[df_accu[b].index[-1], "o_eff"] = "--"
                df_accu[b].at[df_accu[b].index[-1], "SoC"] = ""
            logging.info(f"In- en uitgaande energie per uur batterij "
                         f"{self.battery_options[b]['name']}"
                         f"\n{df_accu[b].to_string(index=False)}")
            for u in range(U+1):
                soc_value = soc[b][u].x
                if b == 0:
                    row_soc = [tijd_soc[u], soc_value, soc_value]
                    df_soc.loc[df_soc.shape[0]] = row_soc
                else:
                    df_soc.at[tijd_soc[u], "soc_"+str(b)] = soc_value

        df_soc.index = pd.to_datetime(df_soc["tijd"])
        sum_cap = 0
        for b in range(B):
            sum_cap += one_soc[b]*100
        for row in df_soc.itertuples():
            sum_soc = 0
            for b in range(B):
                sum_soc += one_soc[b] * row[b+3]
            df_soc.at[row[0], "soc"] = round(100 * sum_soc/sum_cap, 1)

        if not self.debug:
            self.save_df(tablename='prognoses', tijd=tijd_soc, df=df_soc)


        # totaal overzicht
        # pd.options.display.float_format = '{:,.3f}'.format
        cols = ['uur', 'bat_in', 'bat_out']
        cols = cols + ['cons', 'prod', 'base', 'boil', 'wp', 'ev', 'pv_ac', 'cost', 'profit',
                       'b_tem']
        if M > 0:
            cols = cols + ["mach"]
        d_f = pd.DataFrame(columns=cols)
        for u in range(U):
            row = [uur[u], accu_in_sum[u], accu_out_sum[u]]
            row = row + [c_l[u].x, c_t_total[u].x, b_l[u],
                         c_b[u].x, c_hp[u].x, c_ev_sum[u], solar_hour_sum_opt[u], c_l[u].x * pl[u],
                         -c_t_w_tax[u].x * pt[u] - c_t_no_tax[u].x * pt_notax[u],
                         boiler_temp[u + 1].x]
            if M > 0:
                row = row + [c_ma_sum[u]]
            d_f.loc[d_f.shape[0]] = row
        if not self.debug:
            d_f_save = d_f.drop(["uur", "b_tem"], axis=1)
            self.save_df(tablename='prognoses', tijd=tijd, df=d_f_save)
        else:
            logging.info("Berekende prognoses zijn niet opgeslagen.")

        d_f = d_f.astype({"uur": int})
        d_f.loc['total'] = d_f.iloc[:, 1:].sum()
        # d_f.loc['total'] = d_f.loc['total'].astype(object)

        d_f.at[d_f.index[-1], "uur"] = "Totaal"
        d_f.at[d_f.index[-1], "b_tem"] = ""

        logging.info(f"Berekende prognoses: \n{d_f.to_string(index=False)}")
        # , formatters={'uur':'{:03d}'.format}))
        logging.info(f"Winst: {old_cost_da - cost.x:<0.2f} €")

        # doorzetten van alle settings naar HA
        if not self.debug:
            logging.info("Doorzetten van alle settings naar HA")
        else:
            logging.info("Onderstaande settings worden NIET doorgezet naar HA")

        '''
        set helpers output home assistant
        boiler c_b[0].x >0 trigger boiler
        ev     c_ev[0].x > 0 start laden auto, ==0 stop laden auto
        battery multiplus feedin from grid = accu_in[0].x - accu_out[0].x
        '''

        #############################################
        # boiler
        ############################################
        try:
            if self.boiler_present:
                if float(c_b[0].x) > 0.0:
                    if self.debug:
                        logging.info("Boiler opwarmen zou zijn geactiveerd")
                    else:
                        self.call_service(self.boiler_options["activate service"],
                                          entity_id=self.boiler_options["activate entity"])
                        # "input_button.hw_trigger")
                        logging.info("Boiler opwarmen geactiveerd")
                else:
                    logging.info("Boiler opwarmen niet geactiveerd")

            ###########################################
            # ev
            ##########################################
            for e in range(EV):
                if ready_u[e] < U:
                    if self.log_level <= logging.INFO:
                        logging.info(f"Inzet-factor laden {self.ev_options[e]['name']} per stap")
                        print("uur", end=" ")
                        for cs in range(ECS[0]):
                            print(f" {charge_stages[e][cs]['ampere']:4.1f}A", end=" ")
                        print()
                        for u in range(ready_u[e] + 1):
                            print(f"{uur[u]:2d}", end="    ")
                            for cs in range(ECS[0]):
                                print(f"{abs(charger_factor[0][cs][u].x):.2f}", end="   ")
                            print()
                entity_charge_switch = self.ev_options[e]["charge switch"]
                entity_charging_ampere = self.ev_options[e]["entity set charging ampere"]
                entity_stop_laden = self.config.get(["entity stop charging"], self.ev_options[e], None)
                old_switch_state = self.get_state(entity_charge_switch).state
                old_ampere_state = self.get_state(entity_charging_ampere).state
                new_ampere_state = 0
                new_switch_state = "off"
                new_state_stop_laden = None  # "2000-01-01 00:00:00"
                # stop_str = stop_victron.strftime('%Y-%m-%d %H:%M')
                # print()

                # print(uur[0], end="  ")
                for cs in range(ECS[e])[1:]:
                    # print(f"{charger_factor[e][cs][0].x:.2f}", end="  ")
                    if charger_factor[e][cs][0].x > 0:
                        new_ampere_state = charge_stages[e][cs]["ampere"]
                        if new_ampere_state > 0:
                            new_switch_state = "on"
                        if (charger_factor[e][cs][0].x < 1) and (energy_needed[e] > (ev_accu_in[e][0].x + 0.01)):
                            new_ts = start_dt.timestamp() + charger_factor[e][cs][0].x * 3600
                            stop_laden = dt.datetime.fromtimestamp(int(new_ts))
                            new_state_stop_laden = stop_laden.strftime('%Y-%m-%d %H:%M')
                        break
                ev_name = self.ev_options[e]["name"]
                logging.info(f"Berekeningsuitkomst voor opladen van {ev_name}:")
                logging.info(f"- aantal ampere {new_ampere_state}A (was {old_ampere_state}A)")
                logging.info(f"- stand schakelaar '{new_switch_state}' (was '{old_switch_state}')")
                if not (entity_stop_laden is None) and not (new_state_stop_laden is None):
                    logging.info(f"- stop laden op {new_state_stop_laden}")
                logging.info(f"- positie: {ev_position[e]}")
                logging.info(f"- ingeplugd: {ev_plugged_in[e]}")

                if ev_position[e] == "home" and ev_plugged_in[e]:
                    if float(new_ampere_state) > 0.0:
                        if old_switch_state == "off":
                            if self.debug:
                                logging.info(f"Laden van {ev_name} zou zijn aangezet met {new_ampere_state} ampere")
                            else:
                                logging.info(f"Laden van {ev_name} aangezet met {new_ampere_state} ampere via "
                                             f"'{entity_charging_ampere}'")
                                self.set_value(entity_charging_ampere, new_ampere_state)
                                self.turn_on(entity_charge_switch)
                                if not (entity_stop_laden is None) and not (new_state_stop_laden is None):
                                    self.call_service("set_datetime", entity_id=entity_stop_laden,
                                                      datetime=new_state_stop_laden)
                        if old_switch_state == "on":
                            if self.debug:
                                logging.info(f"Laden van {ev_name} zou zijn doorgegaan met {new_ampere_state} A")
                            else:
                                logging.info(f"Laden van {ev_name} is doorgegaan met {new_ampere_state} A")
                                self.set_value(entity_charging_ampere, new_ampere_state)
                                if not (entity_stop_laden is None) and not (new_state_stop_laden is None):
                                    self.call_service("set_datetime", entity_id=entity_stop_laden,
                                                      datetime=new_state_stop_laden)
                    else:
                        if old_switch_state == "on":
                            if self.debug:
                                logging.info(f"Laden van {ev_name} zou zijn uitgezet")
                            else:
                                self.set_value(entity_charging_ampere, 0)
                                self.turn_off(entity_charge_switch)
                                logging.info(f"Laden van {ev_name} uitgezet")
                                if not (entity_stop_laden is None) and not (new_state_stop_laden is None):
                                    self.call_service("set_datetime", entity_id=entity_stop_laden,
                                                      datetime=new_state_stop_laden)
                else:
                    logging.info(f"{ev_name} is niet thuis of niet ingeplugd")
                logging.info(f"Evaluatie status laden {ev_name} op "
                             f""
                             f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M')}")
                logging.info(f"- schakelaar laden: {self.get_state(entity_charge_switch).state}")
                logging.info(f"- aantal ampere: {self.get_state(entity_charging_ampere).state}")

            #######################################
            # solar
            ######################################
            for s in range(solar_num):
                if entity_pv_ac_switch[s] is not None:
                    entity_pv_switch = entity_pv_ac_switch[s]
                    switch_state = self.get_state(entity_pv_switch).state
                    pv_name = self.solar[s]["name"]
                    if (pv_ac_on_off[s][0].x == 1.0) or (solar_prod[s][0] == 0.0):
                        if switch_state == "off":
                            if self.debug:
                                logging.info(f"PV {pv_name} zou zijn aangezet")
                            else:
                                self.turn_on(entity_pv_switch)
                                logging.info(f"PV {pv_name} aangezet")
                    else:
                        if switch_state == "on":
                            if self.debug:
                                logging.info(f"PV {pv_name} zou zijn uitgezet")
                            else:
                                self.turn_off(entity_pv_switch)
                                logging.info(f"PV {pv_name} uitgezet")

            ############################################
            # battery
            ############################################
            for b in range(B):
                # vermogen aan ac kant
                netto_vermogen = int(1000 * (ac_to_dc[b][0].x - ac_from_dc[b][0].x))
                minimum_power = int(self.battery_options[b]["minimum power"])
                bat_name = self.battery_options[b]["name"]
                if abs(netto_vermogen) <= 20:
                    netto_vermogen = 0
                    new_state = "Uit"
                    stop_omvormer = None
                    balance = False
                elif abs(c_l[0].x - c_t_w_tax[0].x - c_t_no_tax[0].x) <= 0.01:
                    new_state = "Aan"
                    balance = True
                    stop_omvormer = None
                elif abs(netto_vermogen) < minimum_power:
                    new_state = "Aan"
                    balance = False
                    new_ts = start_dt.timestamp() + (abs(netto_vermogen) / minimum_power) * 3600
                    stop_omvormer = dt.datetime.fromtimestamp(int(new_ts))
                    if netto_vermogen > 0:
                        netto_vermogen = minimum_power
                    else:
                        netto_vermogen = -minimum_power
                else:
                    new_state = "Aan"
                    balance = False
                    stop_omvormer = None
                if stop_omvormer is None:
                    stop_str = "2000-01-01 00:00:00"
                else:
                    stop_str = stop_omvormer.strftime('%Y-%m-%d %H:%M')
                first_row = df_accu[b].iloc[0]
                from_battery = int(-first_row["dc->"] * 1000 / fraction_first_hour)
                from_pv = int(first_row["pv->dc"] * 1000 / fraction_first_hour)
                from_ac = int(first_row["->dc"] * 1000 / fraction_first_hour)
                calculated_soc = round(soc[b][1].x, 1)

                if self.debug:
                    logging.info(f"Netto vermogen naar(+)/uit(-) batterij {bat_name} zou zijn: {netto_vermogen} W")
                    if stop_omvormer:
                        logging.info(f"tot: {stop_str}")
                    logging.info(f"Balanceren zou zijn: {balance}")
                else:
                    self.set_entity_value("entity set power feedin", self.battery_options[b], netto_vermogen)
                    self.set_entity_option("entity set operating mode", self.battery_options[b], new_state)
                    balance_state = "on" if balance else "off"
                    self.set_entity_state("entity balance switch", self.battery_options[b], balance_state)
                    logging.info(f"Netto vermogen naar(+)/uit(-) omvormer {bat_name}: {netto_vermogen} W"
                                 f"{' tot: '+stop_str if stop_omvormer else ''}")
                    logging.info(f"Balanceren: {balance}{' tot: '+stop_str if stop_omvormer else ''}")
                    helper_id = self.config.get(["entity stop victron"], self.battery_options[b], None)
                    if helper_id is not None:
                        logging.warning(f"The name 'entity stop victron' is deprecated, "
                                        f"please change to 'entity stop inverter'.")
                    if helper_id is None:
                        helper_id = self.config.get(["entity stop inverter"], self.battery_options[b], None)
                    if helper_id is not None:
                        self.call_service("set_datetime", entity_id=helper_id, datetime=stop_str)
                    self.set_entity_value("entity from battery", self.battery_options[b], from_battery)
                    logging.info(f"Vermogen uit batterij: {from_battery}W")
                    self.set_entity_value("entity from pv", self.battery_options[b], from_pv)
                    logging.info(f"Vermogen dat binnenkomt van pv: {from_pv}W")
                    self.set_entity_value("entity from ac", self.battery_options[b], from_ac)
                    logging.info(f"Vermogen dat binnenkomt van ac: {from_ac}W")
                    self.set_entity_value("entity calculated soc", self.battery_options[b], calculated_soc)
                    logging.info(f"Waarde SoC na eerste uur: {calculated_soc}%")

                for s in range(pv_dc_num[b]):
                    entity_pv_switch = self.config.get(["entity pv switch"],
                                                       self.battery_options[b]["solar"][s],
                                                       None)
                    if entity_pv_switch == "":
                        entity_pv_switch = None
                    if entity_pv_switch is not None:
                        switch_state = self.get_state(entity_pv_switch).state
                        pv_name = self.battery_options[b]["solar"][s]["name"]
                        if pv_dc_on_off[b][s][0].x == 1 or pv_prod_dc[b][s][0] == 0.0:
                            if switch_state == "off":
                                if self.debug:
                                    logging.info(f"PV {pv_name} zou zijn aangezet")
                                else:
                                    self.turn_on(entity_pv_switch)
                                    logging.info(f"PV {pv_name} aangezet")
                        else:
                            if switch_state == "on":
                                if self.debug:
                                    logging.info(f"PV {pv_name} zou zijn uitgezet")
                                else:
                                    self.turn_off(entity_pv_switch)
                                    logging.info(f"PV {pv_name} uitgezet")
                            self.turn_on(entity_pv_switch)

            ##################################################
            # heating
            ##################################################
            if self.heater_present:
                entity_curve_adjustment = self.heating_options["entity adjust heating curve"]
                old_adjustment = float(self.get_state(
                    entity_curve_adjustment).state)
                # adjustment factor (K/%) bijv 0.4 K/10% = 0.04
                adjustment_factor = self.heating_options["adjustment factor"]
                adjustment = calc_adjustment_heatcurve(
                    pl[0], p_avg, adjustment_factor, old_adjustment)
                if self.debug:
                    logging.info(f"Aanpassing stooklijn zou zijn: {adjustment:<0.2f}")
                else:
                    logging.info(f"Aanpassing stooklijn: {adjustment:<0.2f}")
                    self.set_value(entity_curve_adjustment, adjustment)

            ########################################################################
            # apparaten /machines
            ########################################################################
            for m in range(M):
                logging.info(f"Apparaat: {ma_name[m]}")
                logging.info(f"Programma: {program_selected[m]}")
                if RL[m] > 0:
                    for r in range(R[m]):
                        if ma_start[m][r].x == 1:
                            # print(f"ma_start: run {r} start {ma_start[m][r].x}")
                            start_machine_str = ma_kw_dt[m][r].strftime('%Y-%m-%d %H:%M')
                            if not (ma_entity_plan_start[m] is None):
                                if self.debug:
                                    logging.info(f"Zou zijn gestart op {start_machine_str}")
                                else:
                                    self.call_service("set_datetime", entity_id=ma_entity_plan_start[m],
                                                      datetime=start_machine_str)
                                    logging.info(f"Start op {start_machine_str}")
                            end_machine_str = ma_kw_dt[m][r + RL[m]].strftime('%Y-%m-%d %H:%M')
                            if not (ma_entity_plan_end[m] is None):
                                if self.debug:
                                    logging.info(f"Zou klaar zijn op {end_machine_str}")
                                else:
                                    self.call_service("set_datetime", entity_id=ma_entity_plan_end[m],
                                                      datetime=end_machine_str)
                                    logging.info(f"Is klaar op {end_machine_str}")

                if self.log_level == logging.DEBUG:
                    logging.debug(f"Per kwartier het berekende verbruik, en het bijbehorende tarief")
                    for kw in range(KW[m]):
                        print(f"kwartier {kw:>2} tijd: {ma_kw_dt[m][kw].strftime('%H:%M')} "
                              f"consumption: {c_ma_kw[m][kw].x:>7.3f} "
                              f"uur: {math.floor(kw / 4)} tarief: {pl[math.floor(kw / 4)]:.4f}")
                    logging.debug(f"Per uur het berekende verbruik, het bijbehorende tarief en de kosten")
                    for u in range(U):
                        print(f"uur {u:>2} tijdstip {tijd[u].strftime('%H:%M')} "
                              f"consumption: {c_ma_u[m][u].x:>7.3f} tarief: {pl[u]:.4f}")

        except Exception as ex:
            error_handling(ex)
            logging.error(f"Onverwachte fout: {ex}")

        #############################################
        # graphs
        #############################################
        accu_in_n = []
        accu_out_p = []
        c_t_n = []
        base_n = []
        boiler_n = []
        heatpump_n = []
        mach_n = []
        ev_n = []
        c_l_p = []
        soc_p = []
        pv_p_org = []
        pv_p_opt = []
        pv_ac_p = []
        max_y = 0
        for u in range(U):
            c_t_n.append(-c_t_total[u].x)
            c_l_p.append(c_l[u].x)
            base_n.append(-b_l[u])
            boiler_n.append(- c_b[u].x)
            heatpump_n.append(-c_hp[u].x)
            ev_n.append(-c_ev_sum[u])
            mach_n.append(-c_ma_sum[u])
            pv_p_org.append(solar_hour_sum_org[u])
            pv_p_opt.append(solar_hour_sum_opt[u])
            pv_ac_p.append(pv_ac_hour_sum[u])
            accu_in_sum = 0
            accu_out_sum = 0
            for b in range(B):
                accu_in_sum += ac_to_dc[b][u].x
                accu_out_sum += ac_from_dc[b][u].x
            accu_in_n.append(-accu_in_sum * hour_fraction[u])
            accu_out_p.append(accu_out_sum * hour_fraction[u])
            max_y = max(max_y, (c_l_p[u] + pv_p_org[u] + pv_ac_p[u]), abs(
                c_t_total[u].x) + b_l[u] + c_b[u].x + c_hp[u].x + c_ev_sum[u] + c_ma_sum[u] + accu_in_sum)
            for b in range(B):
                if u == 0:
                    soc_p.append([])
                soc_p[b].append(soc[b][u].x)
        for b in range(B):
            soc_p[b].append(soc[b][U].x)

        # grafiek 1
        import numpy as np
        from dao.prog.da_graph import GraphBuilder
        gr1_df = pd.DataFrame()
        gr1_df["index"] = np.arange(U)
        gr1_df["uur"] = uur[0:U]
        gr1_df["verbruik"] = c_l_p
        gr1_df["productie"] = c_t_n
        gr1_df["baseload"] = base_n
        gr1_df["boiler"] = boiler_n
        gr1_df["heatpump"] = heatpump_n
        gr1_df["ev"] = ev_n
        gr1_df["mach"] = mach_n
        gr1_df["pv_ac"] = pv_p_opt
        gr1_df["pv_dc"] = pv_ac_p
        gr1_df["accu_in"] = accu_in_n
        gr1_df["accu_out"] = accu_out_p
        style = self.config.get(['graphics', 'style'])
        gr1_options = {
            "title": "Prognose berekend op: " + start_dt.strftime('%Y-%m-%d %H:%M'),
            "style": style,
            "haxis": {
                "values": "uur",
                "title": "uren van de dag"
            },
            "vaxis": [{
                "title": "kWh"
            }
            ],
            "series": [{"column": "verbruik",
                        "type": "stacked",
                        "color": '#00bfff'
                        },
                       {"column": "pv_ac",
                        "title": "PV-AC",
                        "type": "stacked",
                        "color": 'green'
                        },
                       {"column": "accu_out",
                        "title": "Accu out",
                        "type": "stacked",
                        "color": 'red'
                        },
                       {"column": "baseload",
                        "title": "Overig verbr.",
                        "type": "stacked",
                        "color": "#f1a603"
                        },
                       {"column": "boiler",
                        "type": "stacked",
                        "color": '#e39ff6'
                        },
                       {"column": "heatpump",
                        "title": "WP",
                        "type": "stacked",
                        "color": '#a32cc4'
                        },
                       {"column": "ev",
                        "title": "EV",
                        "type": "stacked",
                        "color": 'yellow'
                        },
                       {"column": "mach",
                        "title": "App.",
                        "type": "stacked",
                        "color": 'brown'
                        },
                       {"column": "productie",
                        "title": "Teruglev.",
                        "type": "stacked",
                        "color": '#0080ff'
                        },
                       {"column": "accu_in",
                        "title": "Accu in",
                        "type": "stacked",
                        "color": '#ff8000'
                        },
                       ]
        }
        backend = self.config.get(["graphical backend"], None, "")
        gb = GraphBuilder(backend)
        show_graph = self.config.get(['graphics', 'show'], None, "False").lower() == 'true'
        if show_graph:
            gb.build(gr1_df, gr1_options)

        grid0_df = pd.DataFrame()
        grid0_df["index"] = np.arange(U)
        grid0_df["uur"] = uur[0:U]
        grid0_df["verbruik"] = org_l
        grid0_df["productie"] = org_t
        grid0_df["baseload"] = base_n
        grid0_df["boiler"] = boiler_n
        grid0_df["heatpump"] = heatpump_n
        grid0_df["ev"] = ev_n
        grid0_df["mach"] = mach_n
        grid0_df["pv_ac"] = pv_ac_p
        grid0_df["pv_dc"] = pv_p_org
        style = self.config.get(['graphics', 'style'], None, "default")
        import matplotlib.pyplot as plt
        import matplotlib.ticker as ticker

        plt.set_loglevel(level='warning')
        pil_logger = logging.getLogger('PIL')
        # override the logger logging level to INFO
        pil_logger.setLevel(max(logging.INFO, self.log_level))

        plt.style.use(style)
        fig, axis = plt.subplots(figsize=(8, 9), nrows=3)
        ind = np.arange(U)
        axis[0].bar(ind, np.array(org_l), label='Levering', color='#00bfff', align="edge")
        if sum(pv_p_org) > 0:
            axis[0].bar(ind, np.array(pv_p_org), bottom=np.array(
                org_l), label='PV AC', color='green', align="edge")
        if sum(pv_ac_p) > 0:
            axis[0].bar(ind, np.array(pv_ac_p), bottom=np.array(
                org_l) + np.array(pv_p_org), label='PV DC', color='lime', align="edge")
        axis[0].bar(ind, np.array(base_n),
                    label="Overig verbr.", color='#f1a603', align="edge")
        if self.boiler_present:
            axis[0].bar(ind, np.array(boiler_n), bottom=np.array(
                base_n), label="Boiler", color='#e39ff6', align="edge")
        if self.heater_present:
            axis[0].bar(ind, np.array(heatpump_n), bottom=np.array(
                base_n), label="WP", color='#a32cc4', align="edge")
        axis[0].bar(ind, np.array(ev_n), bottom=np.array(base_n) + np.array(boiler_n) + np.array(heatpump_n),
                    label="EV laden", color='yellow', align="edge")
        if M > 0:
            axis[0].bar(ind, np.array(mach_n),
                        bottom=np.array(base_n) + np.array(boiler_n) + np.array(heatpump_n) + np.array(ev_n),
                        label="Apparatuur", color='brown', align="edge")
        axis[0].bar(ind, np.array(org_t),
                    bottom=np.array(base_n) + np.array(boiler_n) + np.array(heatpump_n) + np.array(ev_n) +
                    np.array(mach_n),
                    label="Teruglev.", color='#0080ff', align="edge")
        axis[0].legend(loc='best', bbox_to_anchor=(1.05, 1.00))
        axis[0].set_ylabel('kWh')
        ylim = math.ceil(max_y)
        # math.ceil(max(max(accu_out_p) + max(c_l_p) + max(pv_p), -min(min(base_n), min(boiler_n),
        # min(heatpump_n), min(ev_n), min(c_t_n), min(accu_in_n) )))
        axis[0].set_ylim([-ylim, ylim])
        axis[0].set_xticks(ind, labels=uur)
        axis[0].xaxis.set_major_locator(ticker.MultipleLocator(2))
        axis[0].xaxis.set_minor_locator(ticker.MultipleLocator(1))
        axis[0].set_title(f"Berekend op: {start_dt.strftime('%d-%m-%Y %H:%M')}\nNiet geoptimaliseerd")

        axis[1].bar(ind, np.array(c_l_p),
                    label='Levering', color='#00bfff', align="edge")
        axis[1].bar(ind, np.array(pv_p_opt), bottom=np.array(
            c_l_p), label='PV AC', color='green', align="edge")
        axis[1].bar(ind, np.array(accu_out_p), bottom=np.array(c_l_p) + np.array(pv_p_opt), label='Accu uit',
                    color='red', align="edge")

        # axis[1].bar(ind, np.array(cons_n), label="Verbruik", color='yellow')
        axis[1].bar(ind, np.array(base_n),
                    label="Overig verbr.", color='#f1a603', align="edge")
        if self.boiler_present:
            axis[1].bar(ind, np.array(boiler_n), bottom=np.array(
                base_n), label="Boiler", color='#e39ff6', align="edge")
        if self.heater_present:
            axis[1].bar(ind, np.array(heatpump_n), bottom=np.array(
                base_n), label="WP", color='#a32cc4', align="edge")
        axis[1].bar(ind, np.array(ev_n), bottom=np.array(base_n) + np.array(boiler_n) + np.array(heatpump_n),
                    label="EV laden", color='yellow', align="edge")
        if M > 0:
            axis[1].bar(ind, np.array(mach_n),
                        bottom=np.array(base_n) + np.array(boiler_n) + np.array(heatpump_n) + np.array(ev_n),
                        label="Apparatuur", color='brown', align="edge")
        axis[1].bar(ind, np.array(c_t_n),
                    bottom=np.array(base_n) + np.array(boiler_n) + np.array(heatpump_n) +
                    np.array(ev_n) + np.array(mach_n),
                    label="Teruglev.", color='#0080ff', align="edge")
        axis[1].bar(ind, np.array(accu_in_n),
                    bottom=np.array(base_n) + np.array(boiler_n) +
                    np.array(heatpump_n) + np.array(ev_n) + np.array(mach_n) + np.array(c_t_n),
                    label='Accu in', color='#ff8000', align="edge")
        axis[1].legend(loc='best', bbox_to_anchor=(1.05, 1.00))
        axis[1].set_ylabel('kWh')
        axis[1].set_ylim([-ylim, ylim])
        axis[1].set_xticks(ind, labels=uur)
        axis[1].xaxis.set_major_locator(ticker.MultipleLocator(2))
        axis[1].xaxis.set_minor_locator(ticker.MultipleLocator(1))
        axis[1].set_title("Day Ahead geoptimaliseerd: " + strategie +
                          ", winst € {:<0.2f}".format(old_cost_da - cost.x))
        axis[1].sharex(axis[0])

        ln1 = []
        line_styles = ["solid", "dashed", "dotted"]
        ind = np.arange(U+1)
        uur.append(24)
        for b in range(B):
            ln1.append(axis[2].plot(ind, soc_p[b], label='SoC ' + self.battery_options[b]["name"],
                       linestyle=line_styles[b], color='red'))
        axis[2].set_xticks(ind, labels=uur)
        axis[2].set_ylabel('% SoC')
        axis[2].set_xlabel("uren van de dag")
        axis[2].xaxis.set_major_locator(ticker.MultipleLocator(2))
        axis[2].xaxis.set_minor_locator(ticker.MultipleLocator(1))
        axis[2].set_ylim([0, 100])
        axis[2].set_title("Verloop SoC en tarieven")
        axis[2].sharex(axis[0])

        axis22 = axis[2].twinx()
        if self.config.get(["graphics", "prices delivery"], None, "true").lower() == "true":
            pl.append(pl[-1])
            ln2 = axis22.step(ind, np.array(pl), label='Tarief\nlevering', color='#00bfff', where='post')
        else:
            ln2 = None
        if self.config.get(["graphics", "prices redelivery"], None, "true").lower() == "true":
            pt_notax.append(pt_notax[-1])
            ln3 = axis22.step(ind, np.array(pt_notax), label="Tarief terug\nno tax", color='#0080ff', where='post')
        else:
            ln3 = None
        if self.config.get(["graphics", "average delivery"], None, "true").lower() == "true":
            pl_avg.append(pl_avg[-1])
            ln4 = axis22.plot(ind, np.array(pl_avg), label="Tarief lev.\ngemid.", linestyle="dashed",
                              color='#00bfff')
        else:
            ln4 = None
        axis22.set_ylabel("euro/kWh")
        axis22.yaxis.set_major_formatter(
            ticker.FormatStrFormatter('% 1.2f'))
        bottom, top = axis22.get_ylim()
        if bottom > 0:
            axis22.set_ylim([0, top])
        lns = []
        for b in range(B):
            lns += ln1[b]
        if ln2:
            lns += ln2
        if ln3:
            lns += ln3
        if ln4:
            lns += ln4
        labels = [line.get_label() for line in lns]
        axis22.legend(lns, labels, loc='best', bbox_to_anchor=(1.40, 1.00))

        plt.subplots_adjust(right=0.75)
        fig.tight_layout()
        plt.savefig("../data/images/calc_" + start_dt.strftime("%Y-%m-%d__%H-%M") + ".png")
        if show_graph:
            plt.show()
        plt.close('all')

    def calc_optimum_debug(self):
        self.debug = True
        self.calc_optimum()


def main():
    """
    main function
    """

    da_calc = DaCalc("../data/options.json")
    if len(sys.argv) > 1:
        args = sys.argv[1:]
        for arg in args:
            if arg.lower() == "debug":
                da_calc.debug = not da_calc.debug
                continue
            if arg.lower() == "calc":
                if da_calc.debug:
                    da_calc.run_task_function("calc_optimum_met_debug")
                else:
                    da_calc.run_task_function("calc_optimum")
                continue
            if arg.lower() == "meteo":
                da_calc.run_task_function("meteo")
                continue
            if arg.lower() == "prices":
                da_calc.run_task_function("prices")
                continue
            if arg.lower() == "tibber":
                da_calc.run_task_function("tibber")
                continue
            if arg.lower() == "clean":
                da_calc.run_task_function("clean")
                continue
            if arg.lower() == "consolidate":
                da_calc.run_task_function("consolidate")
                continue
            if arg.lower() == "calc_baseloads":
                da_calc.run_task_function("calc_baseloads")
                continue
    da_calc.db_da.log_pool_status()


if __name__ == "__main__":
    main()
