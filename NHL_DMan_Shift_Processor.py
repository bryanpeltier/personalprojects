#!pip install xmltodict
import numpy as np
import pandas as pd
from bs4  import BeautifulSoup
import requests
import time
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings("ignore")
import sys
import json
from json import loads, dumps
import lxml
from requests import ConnectionError, ReadTimeout, ConnectTimeout, HTTPError, Timeout
import xml
import re
#from natsort import natsorted
import xml.etree.ElementTree as ET
#import xmltodict
from xml.parsers.expat import ExpatError
from requests.exceptions import ChunkedEncodingError
import matplotlib.pyplot as plt
from tabulate import tabulate

# ewc stands for "Events we care about."
#ewc = ['SHOT', 'HIT', 'BLOCK', 'MISS', 'GIVE', 'TAKE', 'GOAL']
            
def build_sharks_2425_chart():
    games = ('0019', '0037', '0054', '0070', '0073', '0089', '0105', '0117', '0134', '0145', '0154', '0167', '0184', '0204', '0219', '0236', '0240', '0261', '0279', '0293', '0303', '0312', '0344', '0361', '0371', '0387', '0400', '0413')
    player_index = {'38':'Mario Ferraro', '96':'Jake Walman', '4':'Cody Ceci', '26':'Jack Thompson', '37':'Timothy Liljegren', '84':'Jan Rutta', '3':'Henry Thrun'}
    dmen = {'38':[0, 0, 0], '96':[0, 0, 0], '4':[0, 0, 0], '26':[0, 0, 0], '37':[0, 0, 0], '84':[0, 0, 0], '3':[0, 0, 0]}
    table_data = []
    
    for game in (process_scraped_game(scrape_html_events('20242025', x)) for x in games):
        for player in dmen:
            asdt, fcr, games_played = dmen[player]
            asdt_new, fcr_new = calc_zone_exit_time('SJS', player, (parse_indiv_player_shifts('SJS', player, game)))
            dmen[player][0]=asdt+asdt_new
            dmen[player][1]=fcr+fcr_new
            if (asdt_new != 0):
                dmen[player][2] = dmen[player][2]+1
    for player in dmen:
        if dmen[player][2] > 0:  
            dmen[player][0] = dmen[player][0]/dmen[player][2]
            dmen[player][1] = dmen[player][1]/dmen[player][2]
            table_data.append([player, player_index[player], f"{dmen[player][0]:.2f}", f"{dmen[player][1]:.2f}"])
            
        
    x_values = []
    y_values = []
    labels = []

    for player, stats in dmen.items():
        x_values.append(stats[0]) 
        y_values.append(stats[1])  
        labels.append(player)

    plt.scatter(x_values, y_values, color='teal')

    top = False
    for i, label in enumerate(labels):
        if top:
            plt.text(x_values[i], y_values[i] - 3, player_index[label], fontsize=7, ha='center')
            top = False
        else:
            plt.text(x_values[i], y_values[i] + 2, player_index[label], fontsize=7, ha='center')
            top = True
    
    # Set axis labels and title
    plt.xlabel('Avg. Time Taken to Clear the D-Zone (Sec)')
    plt.ylabel('% of Shifts w/o a Successful D-Zone Clearance')
    plt.title('SJS Defensemen D-Zone Metrics 2024-25 Season')
    plt.xlim(min(x_values) - 1, max(x_values) + 1)  
    plt.ylim(20, 80)
    
    plt.show()

    headers = ["Number", "Name", "Avg. Time Taken to Clear the D-Zone (Sec)", "% of Shifts w/o a Successful D-Zone Clearance"]
    return tabulate(table_data, headers=headers, tablefmt="plain") 
    

def calc_indiv_player(player):
    games = ('0019', '0037', '0054', '0070', '0073', '0089', '0105', '0117', '0134', '0145', '0154', '0167', '0184', '0204', '0219', '0236', '0240', '0261', '0279', '0293', '0303', '0312', '0331', '0344', '0361', '0371', '0387', '0400', '0413')
    game_stats = {}
    for game in games:
        processed = process_scraped_game(scrape_html_events('20242025', game))
        asdt_new, fcr_new = calc_zone_exit_time('SJS', player, (parse_indiv_player_shifts('SJS', player, processed)))
        game_stats[game] = [asdt_new, fcr_new]
    

def calc_zone_exit_time(team, num, game):
    #calculate failed-clear rate and avg time to clear
    if game is None:
        return 0, 0
    
    other_team = game.iloc[1]['home_team_abbreviated']
    if team == game.iloc[0]['home_team_abbreviated']:
        other_team = game.iloc[1]['away_team_abbreviated']
        
    game = game.reset_index(drop = True)
    shift_ends = game[game['active'] == False].index.tolist()
    
    total_shift_dzone_time = 0
    num_shifts = 0
    success_clears = 0
    failed_clears = 0
    index_shift_start = 1

    #individual shifts
    for i in shift_ends:
        shift_dzone_time = 0
        has_cleared = False
        desc_orig = game.iloc[index_shift_start-1]['description']
        d_zone = False
        if ((team in desc_orig and 'Def. Zone' in desc_orig) or (other_team in desc_orig and 'Off. Zone' in desc_orig)):
            d_zone = True
        
        #individual events on a single shift
        if index_shift_start == i:
            if d_zone == True:
                failed_clears = failed_clears + 1
                total_shift_dzone_time = total_shift_dzone_time + (game.iloc[i]['game_seconds'] - game.iloc[i-1]['game_seconds'])
        else:   
            for j in range(index_shift_start, i):
                #set variables
                desc = game.iloc[j]['description']
                d_zone_new = False
                if ((team in desc and 'Def. Zone' in desc) or (other_team in desc and 'Off. Zone' in desc)):
                    d_zone_new = True

                #if puck was previously in d-zone and still is in d-zone
                if d_zone == True and d_zone_new == True:
                    shift_dzone_time = shift_dzone_time + (game.iloc[j]['game_seconds'] - game.iloc[j-1]['game_seconds'])
            
                #if puck was previously in d-zone and now has been cleared
                if d_zone == True and d_zone_new == False and game.iloc[j-1]['event'] != 'GOAL':
                    success_clears = success_clears+1
                    has_cleared = True
                
                d_zone = d_zone_new
            #END shift events for loop
        
            if (has_cleared == False):
                failed_clears = failed_clears + 1
            total_shift_dzone_time = total_shift_dzone_time + shift_dzone_time
        num_shifts = num_shifts + 1
        index_shift_start = i + 2
    avg_shift_dzone_time = total_shift_dzone_time/num_shifts
    failed_clear_rate = failed_clears/num_shifts
    return avg_shift_dzone_time, (failed_clear_rate*100)
    

def parse_indiv_player_shifts(team, num, game):
    base = str(num)+'D'
    pattern = r'\b'+base+r'\b'
    if team == game.iloc[0]['home_team_abbreviated']:
        home = True
        if game['home_skaters'].str.contains(pattern).any() == False:
            return None
    else:
        home = False
        if game['away_skaters'].str.contains(pattern).any() == False:
            return None
            
    
    if home == True:
        game['active'] = game['home_skaters'].str.contains(pattern, case = False, na = False)
    else:
        game['active'] = game['away_skaters'].str.contains(pattern, case = False, na = False)

    active_event = game['active'] == True
    imm_follow_active_event = (game['active'] == False) & (game['active'].shift(periods = 1, fill_value = False) == True)
    select = active_event | imm_follow_active_event
    #game[select][['description', 'event_index', 'original_time']].to_excel('check.xlsx', index = False)

    return game[select]
    

def process_scraped_game(game):
    return game
    game.drop(columns = ['home_team', 'away_team', 'event_team', 'other_team', 'event_player_str', 'event_player_1', 'event_player_2', 'event_player_3', 'game_date', 'version'], inplace = True)
    
    game['home_skaters'] = game['home_skaters'].astype(pd.StringDtype()).str.strip()
    game['away_skaters'] = game['away_skaters'].astype(pd.StringDtype()).str.strip()
    game['description'] = game['description'].astype(pd.StringDtype()).str.strip()
    game['event'] = game['event'].astype(pd.StringDtype()).str.strip()
    game['strength'] = game['strength'].astype(pd.StringDtype()).str.strip()
    
    game['home_skaters'] = game['home_skaters'].replace('', pd.NA)
    game.dropna(subset = 'home_skaters', inplace = True)

    game = game[game['event'] != 'STOP']
    
    #return game.dtypes

    return game

def scrape_html_events(season, game_id):
    #global game
    #url = 'https://www.nhl.com/scores/htmlreports/20232024/PL020189.HTM'
    url = 'http://www.nhl.com/scores/htmlreports/' + season + '/PL02' + game_id + '.HTM'
    page = requests.get(url)
    #if int(season)<20092010:
     #   soup = BeautifulSoup(page.content, 'html.parser')
    #else:
     #   soup = BeautifulSoup(page.content, 'lxml')
    soup = BeautifulSoup(page.content.decode('ISO-8859-1'), 'lxml')
    tds = soup.find_all("td", {"class": re.compile('.*bborder.*')})
    #global stripped_html
    #global eventdf
    stripped_html = hs_strip_html(tds)
    length = int(len(stripped_html)/8)
    #eventdf = pd.DataFrame(np.array(stripped_html).reshape(length, 8)).rename(
    eventdf = pd.DataFrame(np.array(stripped_html).reshape(length, 8))
    columns = {0:'index', 1:'period', 2:'strength', 3:'time', 4:'event', 5:'description', 6:'away_skaters', 7:'home_skaters'}
    eventdf.rename(columns, axis = 1, inplace = True)
    split = eventdf['time'].str.split(':')
    game_date = soup.find_all('td', {'align':'center', 'style':'font-size: 10px;font-weight:bold'})[2].get_text()
    
    potentialnames = soup.find_all('td', {'align':'center', 'style':'font-size: 10px;font-weight:bold'})
    
    for i in range(0, 999):
        away = potentialnames[i].get_text()
        if ('Away Game') in away or ('tr./Away') in away:
            away = re.split('Match|Game', away)[0]
            break
        
    for i in range(0, 999):
        home = potentialnames[i].get_text()
        if ('Home Game') in home or ('Dom./Home') in home:
            home = re.split('Match|Game', home)[0]
            break
            
    game = eventdf.assign(away_skaters = eventdf.away_skaters.str.replace('\n', ''),
                  home_skaters = eventdf.home_skaters.str.replace('\n', ''),
                  original_time = eventdf.time,
                  time = split.str[0] + ":" + split.str[1].str[:2],
                  home_team = home,
                  away_team = away)
    
    game = game.assign(away_team_abbreviated = game.away_skaters[0].split(' ')[0],
                       home_team_abbreviated = game.home_skaters[0].split(' ')[0])
    
    game = game[game.period!='Per']
    
    game = game.assign(index = game.index.astype(int)).rename(columns = {'index':'event_index'})
    
    game = game.assign(event_team = game.description.str.split(' ').str[0])
    
    game = game.assign(event_team = game.event_team.str.split('\xa0').str[0])
    
    game = game.assign(event_team = np.where(~game.event_team.isin([game.home_team_abbreviated.iloc[0], game.away_team_abbreviated.iloc[0]]), '\xa0', game.event_team))
    
    game = game.assign(other_team = np.where(game.event_team=='', '\xa0',
                                            np.where(game.event_team==game.home_team_abbreviated.iloc[0], game.away_team_abbreviated.iloc[0], game.home_team_abbreviated.iloc[0])))
    
    game['event_player_str'] = game.description.apply(
    lambda x: re.findall('(#)(\d\d)|(#)(\d)|(-) (\d\d)|(-) (\d)', x)).astype(str
                                                        ).str.replace('#', '').str.replace('-', '').str.replace("'", '').str.replace(',', '').str.replace('(', '').str.replace(')', '').astype(str
                                                        ).str.replace('[', '').str.replace(']', '').apply(lambda x: re.sub(' +', ' ', x)).str.strip()

    game = game.assign(event_player_1 = 
            game.event_player_str.str.split(' ').str[0],
            event_player_2 = 
            game.event_player_str.str.split(' ').str[1],
            event_player_3 = 
            game.event_player_str.str.split(' ').str[2])
    #return game

    if len(game[game.description.str.contains('Drawn By')])>0:
    
        game = game.assign(event_player_2 = np.where(game.description.str.contains('Drawn By'), 
                                          game.description.str.split('Drawn By').str[1].str.split('#').str[1].str.split(' ').str[0].str.strip(), 
                                          game.event_player_2),
                          event_player_3 = np.where(game.description.str.contains('Served By'),
                                                   '\xa0',
                                                   game.event_player_3))

    game = game.assign(event_player_1 = np.where((~pd.isna(game.event_player_1)) & (game.event_player_1!=''),
                              np.where(game.event=='FAC', game.away_team_abbreviated,
                                       game.event_team) + (game.event_player_1.astype(str)), 
                              game.event_player_1),
                  event_player_2 = np.where((~pd.isna(game.event_player_2)) & (game.event_player_2!=''),
                              np.where(game.event=='FAC', game.home_team_abbreviated,
                                       np.where(game.event.isin(['BLOCK', 'HIT', 'PENL']), game.other_team, game.event_team)) + (game.event_player_2.astype(str)), 
                              game.event_player_2),
                  event_player_3 = np.where((~pd.isna(game.event_player_3)) & (game.event_player_3!=''),
                              game.event_team + (game.event_player_3.astype(str)), 
                              game.event_player_3))
    
    game = game.assign(
        event_player_1 = np.where((game.event=='FAC') & (game.event_team==game.home_team_abbreviated),
                                 game.event_player_2, game.event_player_1),
        event_player_2 = np.where((game.event=='FAC') & (game.event_team==game.home_team_abbreviated),
                                 game.event_player_1, game.event_player_2))
    
    #return game
    
    roster = scrape_html_roster(season, game_id).rename(columns = {'Nom/Name':'Name'})
    roster = roster[roster.status=='player']
    roster = roster.assign(team_abbreviated = np.where(roster.team=='home', 
                                                       game.home_team_abbreviated.iloc[0],
                                                      game.away_team_abbreviated.iloc[0]))

    roster = roster.assign(teamnum = roster.team_abbreviated + roster['#'],
                          Name = roster.Name.str.split('(').str[0].str.strip())
    
    event_player_1s = roster.loc[:, ['teamnum', 'Name']].rename(columns = {'teamnum':'event_player_1', 'Name':'ep1_name'})
    event_player_2s = roster.loc[:, ['teamnum', 'Name']].rename(columns = {'teamnum':'event_player_2', 'Name':'ep2_name'})
    event_player_3s = roster.loc[:, ['teamnum', 'Name']].rename(columns = {'teamnum':'event_player_3', 'Name':'ep3_name'})
    
    game = game.merge(
    event_player_1s, on = 'event_player_1', how = 'left').merge(
    event_player_2s, on = 'event_player_2', how = 'left').merge(
    event_player_3s, on = 'event_player_3', how = 'left').assign(
    date = game_date)
    #return game
    game['period'] = np.where(game['period'] == '', '1', game['period'])
    game['time'] = np.where((game['time'] == '') | (pd.isna(game['time'])), '0:00', game['time'])
    game['period'] = game.period.astype(int)

    game['period_seconds'] = game.time.str.split(':').str[0].str.replace('-', '').astype(int) * 60 + game.time.str.split(':').str[1].str.replace('-', '').astype(int)

    game['game_seconds'] = (np.where((game.period<5) & int(game_id[0])!=3, 
                                       (((game.period - 1) * 1200) + game.period_seconds),
                              3900))
    
    game = game.assign(priority = np.where(game.event.isin(['TAKE', 'GIVE', 'MISS', 'HIT', 'SHOT', 'BLOCK']), 1, 
                                            np.where(game.event=="GOAL", 2,
                                                np.where(game.event=="STOP", 3,
                                                    np.where(game.event=="DELPEN", 4,
                                                        np.where(game.event=="PENL", 5,
                                                            np.where(game.event=="CHANGE", 6,
                                                                np.where(game.event=="PEND", 7,
                                                                    np.where(game.event=="GEND", 8,
                                                                        np.where(game.event=="FAC", 9, 0)))))))))).sort_values(by = ['game_seconds', 'period', 'event_player_1', 'event'])
    game = game.assign(version = 
                       (np.where(
                       (game.event==game.event.shift()) & 
                       (game.event_player_1==game.event_player_1.shift()) &
                       (game.event_player_1!='') &
                       (game.game_seconds==game.game_seconds.shift()),
                        1, 0)))
    
    game = game.assign(version = 
                           (np.where(
                           (game.event==game.event.shift(2)) & 
                           (game.event_player_1==game.event_player_1.shift(2)) &
                           (game.game_seconds==game.game_seconds.shift(2)) & 
                           (game.event_player_1!='') &
                           (~game.description.str.contains('Penalty Shot')),
                            2, game.version)))
    
    game = game.assign(version = 
                           (np.where(
                           (game.event==game.event.shift(3)) & 
                           (game.event_player_1==game.event_player_1.shift(3)) &
                           (game.game_seconds==game.game_seconds.shift(3)) & 
                           (game.event_player_1!=''),
                            3, game.version)))
    
    game = game.assign(date = pd.to_datetime(game.date[~pd.isna(game.date)].iloc[0])
                  ).rename(columns = {'date':'game_date'}).sort_values(by = ['event_index'])
    
    game = game.assign(event_player_1 = game.ep1_name, event_player_2 = game.ep2_name, event_player_3 = game.ep3_name).drop(columns = ['ep1_name', 'ep2_name', 'ep3_name'])
    
    game = game.assign(home_team = np.where(game.home_team=='CANADIENS MONTREAL', 'MONTREAL CANADIENS', game.home_team),
                      away_team = np.where(game.away_team=='CANADIENS MONTREAL', 'MONTREAL CANADIENS', game.away_team))
    
    if int(game_id[0])!=3:
        game = game[game.game_seconds<4000]
    
    game['game_date'] = np.where((season=='20072008') & (game_id == '20003'), game.game_date + pd.Timedelta(days=1), game.game_date)
    
    game = game.assign(event_player_1 = np.where((game.description.str.upper().str.contains('TEAM')) | (game.description.str.lower().str.contains('bench')),
                                     'BENCH',
                                     game.event_player_1))
    
    game = game.assign(home_skater_count_temp = (game.home_skaters.apply(lambda x: len(re.findall('[A-Z]', x)))),
          away_skater_count_temp = (game.away_skaters.apply(lambda x: len(re.findall('[A-Z]', x))))
         )
    
    game = game.assign(event_team = np.where((game.event=='PENL') & (game.event_team=='') & (game.description.str.lower().str.contains('bench')) & (game.home_skater_count_temp>game.home_skater_count_temp.shift(-1)),
                                game.home_team_abbreviated, game.event_team))

    game = game.assign(event_team = np.where((game.event=='PENL') & (game.event_team=='') & (game.description.str.lower().str.contains('bench')) & (game.away_skater_count_temp>game.away_skater_count_temp.shift(-1)),
                                game.away_team_abbreviated, game.event_team))
    
    return game.drop(columns = ['period_seconds', 'time', 'priority', 'home_skater_count_temp', 'away_skater_count_temp'])

def hs_strip_html(td):
    """
    Function from Harry Shomer's Github
    
    Strip html tags and such 
    
    :param td: pbp
    
    :return: list of plays (which contain a list of info) stripped of html
    """
    for y in range(len(td)):
        # Get the 'br' tag for the time column...this get's us time remaining instead of elapsed and remaining combined
        if y == 3:
            td[y] = td[y].get_text()   # This gets us elapsed and remaining combined-< 3:0017:00
            index = td[y].find(':')
            td[y] = td[y][:index+3]
        elif (y == 6 or y == 7) and td[0] != '#':
            # 6 & 7-> These are the player 1 ice one's
            # The second statement controls for when it's just a header
            baz = td[y].find_all('td')
            bar = [baz[z] for z in range(len(baz)) if z % 4 != 0]  # Because of previous step we get repeats...delete some

            # The setup in the list is now: Name/Number->Position->Blank...and repeat
            # Now strip all the html
            players = []
            for i in range(len(bar)):
                if i % 3 == 0:
                    try:
                        name = return_name_html(bar[i].find('font')['title'])
                        number = bar[i].get_text().strip('\n')  # Get number and strip leading/trailing newlines
                    except KeyError:
                        name = ''
                        number = ''
                elif i % 3 == 1:
                    if name != '':
                        position = bar[i].get_text()
                        players._append([name, number, position])

            td[y] = players
        else:
            td[y] = td[y].get_text()

    return td

def scrape_html_roster(season, game_id):
    url = 'http://www.nhl.com/scores/htmlreports/' + season + '/RO02' + game_id + '.HTM'
    #url = 'https://www.nhl.com/scores/htmlreports/20232024/RO020189.HTM'
    page = requests.get(url)
    soup = BeautifulSoup(page.content.decode('ISO-8859-1'), 'lxml', multi_valued_attributes = None)

    teamsoup = soup.find_all('td', {'align':'center', 'class':['teamHeading + border', 'teamHeading + border '], 'width':'50%'})
    
    away_team = teamsoup[0].get_text()
    home_team = teamsoup[1].get_text()
    
    home_player_soup = (soup.find_all('table', {'align':'center', 'border':'0', 'cellpadding':'0', 
                        'cellspacing':'0', 'width':'100%', 'xmlns:ext':''}))[1].find_all('td')

    length = int(len(home_player_soup)/3)

    home_player_df = pd.DataFrame(np.array(home_player_soup).reshape(length, 3))

    home_player_df.columns = home_player_df.iloc[0]

    home_player_df = home_player_df.drop(0).assign(team = 'home', team_name = home_team)

    away_player_soup = (soup.find_all('table', {'align':'center', 'border':'0', 'cellpadding':'0', 
                            'cellspacing':'0', 'width':'100%', 'xmlns:ext':''}))[0].find_all('td')

    length = int(len(away_player_soup)/3)

    away_player_df = pd.DataFrame(np.array(away_player_soup).reshape(length, 3))

    away_player_df.columns = away_player_df.iloc[0]

    away_player_df = away_player_df.drop(0).assign(team = 'away', team_name = away_team)
    
    #global home_scratch_soup
    
    if len(soup.find_all('table', {'align':'center', 'border':'0', 'cellpadding':'0', 
                            'cellspacing':'0', 'width':'100%', 'xmlns:ext':''}))>3:

        home_scratch_soup = (soup.find_all('table', {'align':'center', 'border':'0', 'cellpadding':'0', 
                                'cellspacing':'0', 'width':'100%', 'xmlns:ext':''}))[3].find_all('td')
    
        if len(home_scratch_soup)>1:

            length = int(len(home_scratch_soup)/3)

            home_scratch_df = pd.DataFrame(np.array(home_scratch_soup).reshape(length, 3))

            home_scratch_df.columns = home_scratch_df.iloc[0]

            home_scratch_df = home_scratch_df.drop(0).assign(team = 'home', team_name = home_team)

    if 'home_scratch_df' not in locals():
        
        home_scratch_df = pd.DataFrame()
        
    if len(soup.find_all('table', {'align':'center', 'border':'0', 'cellpadding':'0', 
                            'cellspacing':'0', 'width':'100%', 'xmlns:ext':''}))>2:
    
        away_scratch_soup = (soup.find_all('table', {'align':'center', 'border':'0', 'cellpadding':'0', 
                                'cellspacing':'0', 'width':'100%', 'xmlns:ext':''}))[2].find_all('td')
        
        if len(away_scratch_soup)>1:

            length = int(len(away_scratch_soup)/3)

            away_scratch_df = pd.DataFrame(np.array(away_scratch_soup).reshape(length, 3))

            away_scratch_df.columns = away_scratch_df.iloc[0]

            away_scratch_df = away_scratch_df.drop(0).assign(team = 'away', team_name = away_team)
        
    if 'away_scratch_df' not in locals():
        
        away_scratch_df = pd.DataFrame()

    player_df = pd.concat([home_player_df, away_player_df]).assign(status = 'player')
    scratch_df = pd.concat([home_scratch_df, away_scratch_df]).assign(status = 'scratch')
    roster_df = pd.concat([player_df, scratch_df])
    
    roster_df = roster_df.assign(team = np.where(roster_df.team=='CANADIENS MONTREAL', 'MONTREAL CANADIENS', roster_df.team))
    
    # FIX NAMES

    roster_df = roster_df.rename(columns = {'Nom/Name':'Name'})
    
    roster_df.Name = roster_df.Name.str.split('(').str[0].str.strip()
    
    # Max Pacioretty doesn't exist in ESPN in 2009-2010, sadly.
    
    roster_df['Name'] = np.where(roster_df['Name'].str.contains('ALEXANDRE '), 
                                roster_df.Name.str.replace('ALEXANDRE ', 'ALEX '),
                                roster_df['Name'])
    
    roster_df['Name'] = np.where(roster_df['Name'].str.contains('ALEXANDER '), 
                                roster_df.Name.str.replace('ALEXANDER ', 'ALEX '),
                                roster_df['Name'])
    
    roster_df['Name'] = np.where(roster_df['Name'].str.contains('CHRISTOPHER '), 
                                roster_df.Name.str.replace('CHRISTOPHER ', 'CHRIS '),
                                roster_df['Name'])
    
    # List of names and fixed from Evolving Hockey Scraper.
    
    roster_df = roster_df.assign(Name = 
    (np.where(roster_df['Name']== "ANDREI KASTSITSYN" , "ANDREI KOSTITSYN",
    (np.where(roster_df['Name']== "AJ GREER" , "A.J. GREER",
    (np.where(roster_df['Name']== "ANDREW GREENE" , "ANDY GREENE",
    (np.where(roster_df['Name']== "ANDREW WOZNIEWSKI" , "ANDY WOZNIEWSKI", 
    (np.where(roster_df['Name']== "ANTHONY DEANGELO" , "TONY DEANGELO",
    (np.where(roster_df['Name']== "BATES (JON) BATTAGLIA" , "BATES BATTAGLIA",
    (np.where(roster_df['Name'].isin(["BJ CROMBEEN", "B.J CROMBEEN", "BRANDON CROMBEEN", "B J CROMBEEN"]) , "B.J. CROMBEEN", 
    (np.where(roster_df['Name']== "BRADLEY MILLS" , "BRAD MILLS",
    (np.where(roster_df['Name']== "CAMERON BARKER" , "CAM BARKER", 
    (np.where(roster_df['Name']== "COLIN (JOHN) WHITE" , "COLIN WHITE",
    (np.where(roster_df['Name']== "CRISTOVAL NIEVES" , "BOO NIEVES",
    (np.where(roster_df['Name']== "CHRIS VANDE VELDE" , "CHRIS VANDEVELDE", 
    (np.where(roster_df['Name']== "DANNY BRIERE" , "DANIEL BRIERE",
    (np.where(roster_df['Name'].isin(["DAN CLEARY", "DANNY CLEARY"]) , "DANIEL CLEARY",
    (np.where(roster_df['Name']== "DANIEL GIRARDI" , "DAN GIRARDI", 
    (np.where(roster_df['Name']== "DANNY O'REGAN" , "DANIEL O'REGAN",
    (np.where(roster_df['Name']== "DANIEL CARCILLO" , "DAN CARCILLO", 
    (np.where(roster_df['Name']== "DAVID JOHNNY ODUYA" , "JOHNNY ODUYA", 
    (np.where(roster_df['Name']== "DAVID BOLLAND" , "DAVE BOLLAND", 
    (np.where(roster_df['Name']== "DENIS JR. GAUTHIER" , "DENIS GAUTHIER",
    (np.where(roster_df['Name']== "DWAYNE KING" , "DJ KING", 
    (np.where(roster_df['Name']== "EDWARD PURCELL" , "TEDDY PURCELL", 
    (np.where(roster_df['Name']== "EMMANUEL FERNANDEZ" , "MANNY FERNANDEZ", 
    (np.where(roster_df['Name']== "EMMANUEL LEGACE" , "MANNY LEGACE", 
    (np.where(roster_df['Name']== "EVGENII DADONOV" , "EVGENY DADONOV", 
    (np.where(roster_df['Name']== "FREDDY MODIN" , "FREDRIK MODIN", 
    (np.where(roster_df['Name']== "FREDERICK MEYER IV" , "FREDDY MEYER",
    (np.where(roster_df['Name']== "HARRISON ZOLNIERCZYK" , "HARRY ZOLNIERCZYK", 
    (np.where(roster_df['Name']== "ILJA BRYZGALOV" , "ILYA BRYZGALOV", 
    (np.where(roster_df['Name']== "JACOB DOWELL" , "JAKE DOWELL",
    (np.where(roster_df['Name']== "JAMES HOWARD" , "JIMMY HOWARD", 
    (np.where(roster_df['Name']== "JAMES VANDERMEER" , "JIM VANDERMEER",
    (np.where(roster_df['Name']== "JAMES WYMAN" , "JT WYMAN",
    (np.where(roster_df['Name']== "JOHN HILLEN III" , "JACK HILLEN",
    (np.where(roster_df['Name']== "JOHN ODUYA" , "JOHNNY ODUYA",
    (np.where(roster_df['Name']== "JOHN PEVERLEY" , "RICH PEVERLEY",
    (np.where(roster_df['Name']== "JONATHAN SIM" , "JON SIM",
    (np.where(roster_df['Name']== "JONATHON KALINSKI" , "JON KALINSKI",
    (np.where(roster_df['Name']== "JONATHAN AUDY-MARCHESSAULT" , "JONATHAN MARCHESSAULT", 
    (np.where(roster_df['Name']== "JOSEPH CRABB" , "JOEY CRABB",
    (np.where(roster_df['Name']== "JOSEPH CORVO" , "JOE CORVO", 
    (np.where(roster_df['Name']== "JOSHUA BAILEY" , "JOSH BAILEY",
    (np.where(roster_df['Name']== "JOSHUA HENNESSY" , "JOSH HENNESSY", 
    (np.where(roster_df['Name']== "JOSHUA MORRISSEY" , "JOSH MORRISSEY",
    (np.where(roster_df['Name']== "JEAN-FRANCOIS JACQUES" , "J-F JACQUES", 
    (np.where(roster_df['Name'].isin(["J P DUMONT", "JEAN-PIERRE DUMONT"]) , "J-P DUMONT", 
    (np.where(roster_df['Name']== "JT COMPHER" , "J.T. COMPHER",
    (np.where(roster_df['Name']== "KRISTOPHER LETANG" , "KRIS LETANG", 
    (np.where(roster_df['Name']== "KRYSTOFER BARCH" , "KRYS BARCH", 
    (np.where(roster_df['Name']== "KRYSTOFER KOLANOS" , "KRYS KOLANOS",
    (np.where(roster_df['Name']== "MARC POULIOT" , "MARC-ANTOINE POULIOT",
    (np.where(roster_df['Name']== "MARTIN ST LOUIS" , "MARTIN ST. LOUIS", 
    (np.where(roster_df['Name']== "MARTIN ST PIERRE" , "MARTIN ST. PIERRE",
    (np.where(roster_df['Name']== "MARTY HAVLAT" , "MARTIN HAVLAT",
    (np.where(roster_df['Name']== "MATTHEW CARLE" , "MATT CARLE", 
    (np.where(roster_df['Name']== "MATHEW DUMBA" , "MATT DUMBA",
    (np.where(roster_df['Name']== "MATTHEW BENNING" , "MATT BENNING", 
    (np.where(roster_df['Name']== "MATTHEW IRWIN" , "MATT IRWIN",
    (np.where(roster_df['Name']== "MATTHEW NIETO" , "MATT NIETO",
    (np.where(roster_df['Name']== "MATTHEW STAJAN" , "MATT STAJAN",
    (np.where(roster_df['Name']== "MAXIM MAYOROV" , "MAKSIM MAYOROV",
    (np.where(roster_df['Name']== "MAXIME TALBOT" , "MAX TALBOT", 
    (np.where(roster_df['Name']== "MAXWELL REINHART" , "MAX REINHART",
    (np.where(roster_df['Name']== "MICHAEL BLUNDEN" , "MIKE BLUNDEN",
    (np.where(roster_df['Name'].isin(["MICHAËL BOURNIVAL", "MICHAÃ\x8bL BOURNIVAL"]), "MICHAEL BOURNIVAL",
    (np.where(roster_df['Name']== "MICHAEL CAMMALLERI" , "MIKE CAMMALLERI", 
    (np.where(roster_df['Name']== "MICHAEL FERLAND" , "MICHEAL FERLAND", 
    (np.where(roster_df['Name']== "MICHAEL GRIER" , "MIKE GRIER",
    (np.where(roster_df['Name']== "MICHAEL KNUBLE" , "MIKE KNUBLE",
    (np.where(roster_df['Name']== "MICHAEL KOMISAREK" , "MIKE KOMISAREK",
    (np.where(roster_df['Name']== "MICHAEL MATHESON" , "MIKE MATHESON",
    (np.where(roster_df['Name']== "MICHAEL MODANO" , "MIKE MODANO",
    (np.where(roster_df['Name']== "MICHAEL RUPP" , "MIKE RUPP",
    (np.where(roster_df['Name']== "MICHAEL SANTORELLI" , "MIKE SANTORELLI", 
    (np.where(roster_df['Name']== "MICHAEL SILLINGER" , "MIKE SILLINGER",
    (np.where(roster_df['Name']== "MITCHELL MARNER" , "MITCH MARNER", 
    (np.where(roster_df['Name']== "NATHAN GUENIN" , "NATE GUENIN",
    (np.where(roster_df['Name']== "NICHOLAS BOYNTON" , "NICK BOYNTON",
    (np.where(roster_df['Name']== "NICHOLAS DRAZENOVIC" , "NICK DRAZENOVIC", 
    (np.where(roster_df['Name']== "NICKLAS BERGFORS" , "NICLAS BERGFORS",
    (np.where(roster_df['Name']== "NICKLAS GROSSMAN" , "NICKLAS GROSSMANN", 
    (np.where(roster_df['Name']== "NICOLAS PETAN" , "NIC PETAN", 
    (np.where(roster_df['Name']== "NIKLAS KRONVALL" , "NIKLAS KRONWALL",
    (np.where(roster_df['Name']== "NIKOLAI ANTROPOV" , "NIK ANTROPOV",
    (np.where(roster_df['Name']== "NIKOLAI KULEMIN" , "NIKOLAY KULEMIN", 
    (np.where(roster_df['Name']== "NIKOLAI ZHERDEV" , "NIKOLAY ZHERDEV",
    (np.where(roster_df['Name']== "OLIVIER MAGNAN-GRENIER" , "OLIVIER MAGNAN",
    (np.where(roster_df['Name']== "PAT MAROON" , "PATRICK MAROON", 
    (np.where(roster_df['Name'].isin(["P. J. AXELSSON", "PER JOHAN AXELSSON"]) , "P.J. AXELSSON",
    (np.where(roster_df['Name'].isin(["PK SUBBAN", "P.K SUBBAN"]) , "P.K. SUBBAN", 
    (np.where(roster_df['Name'].isin(["PIERRE PARENTEAU", "PIERRE-ALEX PARENTEAU", "PIERRE-ALEXANDRE PARENTEAU", "PA PARENTEAU", "P.A PARENTEAU", "P-A PARENTEAU"]) , "P.A. PARENTEAU", 
    (np.where(roster_df['Name']== "PHILIP VARONE" , "PHIL VARONE",
    (np.where(roster_df['Name']== "QUINTIN HUGHES" , "QUINN HUGHES",
    (np.where(roster_df['Name']== "RAYMOND MACIAS" , "RAY MACIAS",
    (np.where(roster_df['Name']== "RJ UMBERGER" , "R.J. UMBERGER",
    (np.where(roster_df['Name']== "ROBERT BLAKE" , "ROB BLAKE",
    (np.where(roster_df['Name']== "ROBERT EARL" , "ROBBIE EARL",
    (np.where(roster_df['Name']== "ROBERT HOLIK" , "BOBBY HOLIK",
    (np.where(roster_df['Name']== "ROBERT SCUDERI" , "ROB SCUDERI",
    roster_df['Name']))))))))))))))))))))))))))))))))))))))))))))))))))))))
    )))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))))
    ))))))))))
    
    roster_df['Name'] = (np.where(roster_df['Name']== "RODNEY PELLEY" , "ROD PELLEY",
    (np.where(roster_df['Name']== "SIARHEI KASTSITSYN" , "SERGEI KOSTITSYN",
    (np.where(roster_df['Name']== "SIMEON VARLAMOV" , "SEMYON VARLAMOV", 
    (np.where(roster_df['Name']== "STAFFAN KRONVALL" , "STAFFAN KRONWALL",
    (np.where(roster_df['Name']== "STEVEN REINPRECHT" , "STEVE REINPRECHT",
    (np.where(roster_df['Name']== "TJ GALIARDI" , "T.J. GALIARDI",
    (np.where(roster_df['Name']== "TJ HENSICK" , "T.J. HENSICK",
    (np.where(roster_df['Name'].isin(["TJ OSHIE", "T.J OSHIE"]) , "T.J. OSHIE", 
    (np.where(roster_df['Name']== "TOBY ENSTROM" , "TOBIAS ENSTROM", 
    (np.where(roster_df['Name']== "TOMMY SESTITO" , "TOM SESTITO",
    (np.where(roster_df['Name']== "VACLAV PROSPAL" , "VINNY PROSPAL",
    (np.where(roster_df['Name']== "VINCENT HINOSTROZA" , "VINNIE HINOSTROZA",
    (np.where(roster_df['Name']== "WILLIAM THOMAS" , "BILL THOMAS",
    (np.where(roster_df['Name']== "ZACHARY ASTON-REESE" , "ZACH ASTON-REESE",
    (np.where(roster_df['Name']== "ZACHARY SANFORD" , "ZACH SANFORD",
    (np.where(roster_df['Name']== "ZACHERY STORTINI" , "ZACK STORTINI",
    (np.where(roster_df['Name']== "MATTHEW MURRAY" , "MATT MURRAY",
    (np.where(roster_df['Name']== "J-SEBASTIEN AUBIN" , "JEAN-SEBASTIEN AUBIN",
    (np.where(roster_df['Name'].isin(["J.F. BERUBE", "JEAN-FRANCOIS BERUBE"]) , "J-F BERUBE", 
    (np.where(roster_df['Name']== "JEFF DROUIN-DESLAURIERS" , "JEFF DESLAURIERS", 
    (np.where(roster_df['Name']== "NICHOLAS BAPTISTE" , "NICK BAPTISTE",
    (np.where(roster_df['Name']== "OLAF KOLZIG" , "OLIE KOLZIG",
    (np.where(roster_df['Name']== "STEPHEN VALIQUETTE" , "STEVE VALIQUETTE",
    (np.where(roster_df['Name']== "THOMAS MCCOLLUM" , "TOM MCCOLLUM",
    (np.where(roster_df['Name']== "TIMOTHY JR. THOMAS" , "TIM THOMAS",
    (np.where(roster_df['Name']== "TIM GETTINGER" , "TIMOTHY GETTINGER",
    (np.where(roster_df['Name']== "NICHOLAS SHORE" , "NICK SHORE",
    (np.where(roster_df['Name']== "T.J. TYNAN" , "TJ TYNAN",
    (np.where(roster_df['Name']== "ALEXIS LAFRENI?RE" , "ALEXIS LAFRENIÈRE",
    (np.where(roster_df['Name']== "ALEXIS LAFRENIERE" , "ALEXIS LAFRENIÈRE", 
    (np.where(roster_df['Name']== "ALEXIS LAFRENIÃRE" , "ALEXIS LAFRENIÈRE",
    (np.where(roster_df['Name']== "TIM STUTZLE" , "TIM STÜTZLE",
    (np.where(roster_df['Name']== "TIM ST?TZLE" , "TIM STÜTZLE",
    (np.where(roster_df['Name']== "TIM STÃTZLE" , "TIM STÜTZLE",
    (np.where(roster_df['Name']== "JANI HAKANPÃ\x84Ã\x84" , "JANI HAKANPAA",
    (np.where(roster_df['Name']== "EGOR SHARANGOVICH" , "YEGOR SHARANGOVICH",
    (np.where(roster_df['Name']== "CALLAN FOOTE" , "CAL FOOTE",
    (np.where(roster_df['Name']== "MATTIAS JANMARK-NYLEN" , "MATTIAS JANMARK",
    (np.where(roster_df['Name']== "JOSH DUNNE" , "JOSHUA DUNNE",roster_df['Name'])))))))))))))))))))))))))))))))))))))))))))
    )))))))))))))))))))))))))))))))))))
    
    roster_df['Name'] = np.where((roster_df['Name']=="SEBASTIAN AHO") & (roster_df['Pos']=='D'), 'SEBASTIAN AHO SWE', roster_df['Name'])
    roster_df['Name'] = np.where((roster_df['Name']=="COLIN WHITE") & (roster_df['Pos']=='D'), 'COLIN WHITE CAN', roster_df['Name'])
    roster_df['Name'] = np.where((roster_df['Name']=="SEAN COLLINS") & (roster_df['Pos']=='D'), 'SEAN COLLINS CAN', roster_df['Name'])
    roster_df['Name'] = np.where((roster_df['Name']=="ALEX PICARD") & (roster_df['Pos']!='D'), 'ALEX PICARD F', roster_df['Name'])
    roster_df['Name'] = np.where((roster_df['Name']=="ERIK GUSTAFSSON") & (int(season)<20132014), 'ERIK GUSTAFSSON 88', roster_df['Name'])
    roster_df['Name'] = np.where((roster_df['Name']=="MIKKO LEHTONEN") & (int(season)<20202021), 'MIKKO LEHTONEN F', roster_df['Name'])
    roster_df['Name'] = np.where(roster_df['Name']=='ALEX BARRÃ-BOULET', 'ALEX BARRE-BOULET', roster_df['Name'])
    roster_df['Name'] = np.where(roster_df['Name']=='COLIN', 'COLIN WHITE CAN', roster_df['Name'])

    roster_df['Name'] = (np.where(roster_df['Name']== "JANIS MOSER" , "J.J. MOSER",
        (np.where(roster_df['Name']== "NICHOLAS PAUL" , "NICK PAUL",
        (np.where(roster_df['Name']== "JACOB MIDDLETON" , "JAKE MIDDLETON",
        (np.where(roster_df['Name']== "TOMMY NOVAK" , "THOMAS NOVAK",
        roster_df['Name']))))))))

    roster_df['Name'] = roster_df['Name'].str.normalize('NFKD').str.encode('ascii', errors='ignore').str.decode('utf-8').str.upper()

    return roster_df


