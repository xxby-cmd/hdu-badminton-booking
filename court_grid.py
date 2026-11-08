"""Keyboard/mouse selectable person-icon court grid, with disabled-state feedback."""
import tkinter as tk
from tkinter import ttk
from booking import BookingError
from selection_plan import SelectedSlot

class CourtGrid(ttk.Frame):
    CELL_W=60;CELL_H=54;TIME_W=114;HEAD_H=0
    def __init__(self,parent,on_change,on_hint):
        super().__init__(parent)
        self.model=None;self.on_change=on_change;self.on_hint=on_hint;self.enabled=True
        self.header=tk.Canvas(self,height=36,bg='#eff4fb',highlightthickness=0)
        self.header.grid(row=0,column=0,sticky='ew')
        self.canvas=tk.Canvas(self,bg='white',highlightthickness=1,highlightbackground='#dbe3ef',takefocus=True)
        self.ybar=ttk.Scrollbar(self,orient='vertical',command=self.canvas.yview)
        self.xbar=ttk.Scrollbar(self,orient='horizontal',command=self.scroll_x)
        self.canvas.configure(yscrollcommand=self.ybar.set,xscrollcommand=self.xbar.set)
        self.canvas.grid(row=1,column=0,sticky='nsew');self.ybar.grid(row=1,column=1,sticky='ns')
        self.xbar.grid(row=2,column=0,sticky='ew')
        self.rowconfigure(1,weight=1);self.columnconfigure(0,weight=1)
        self.canvas.bind('<Button-1>',self.click);self.canvas.bind('<Motion>',self.hover)
        self.canvas.bind('<MouseWheel>',lambda e:self.canvas.yview_scroll(-int(e.delta/120),'units'))
        self.canvas.bind('<space>',self.activate);self.canvas.bind('<Return>',self.activate)
        for key,delta in (('Left',(0,-1)),('Right',(0,1)),('Up',(-1,0)),('Down',(1,0))):
            self.canvas.bind('<'+key+'>',lambda e,d=delta:self.move(*d))
        self.focus_cell=(0,0)
        self.clear('登录并读取七日排期后，在这里点击格子')
    def clear(self,text):
        self.model=None;self.canvas.delete('all');self.header.delete('all')
        self.canvas.create_text(25,30,anchor='nw',text=text,fill='#64748b',font=('Microsoft YaHei UI',12),width=650)
    def scroll_x(self,*args):
        self.canvas.xview(*args);self.header.xview(*args)
    def set_model(self,model):
        self.model=model;self.focus_cell=(0,0);self.draw()
    def slots(self):
        sites=[r['site_id'] for r in self.model.stock['site_situation']]
        times=self.model.meta['time_list']
        return sites,times
    def at(self,x,y):
        if self.model is None:return None
        sites,times=self.slots()
        col=int((self.canvas.canvasx(x)-self.TIME_W)//self.CELL_W)
        row=int((self.canvas.canvasy(y)-self.HEAD_H)//self.CELL_H)
        if 0<=row<len(times)-1 and 0<=col<len(sites):return row,col,SelectedSlot(times[row],times[row+1],sites[col])
    def person(self,x,y,color):
        c=self.canvas
        c.create_oval(x-4,y-13,x+4,y-5,fill=color,outline=color)
        c.create_line(x,y-3,x,y+8,fill=color,width=3)
        c.create_line(x-7,y+2,x,y-2,x+7,y+2,fill=color,width=3)
        c.create_line(x-6,y+15,x,y+7,x+6,y+15,fill=color,width=3)
    def draw(self):
        if self.model is None:return
        c=self.canvas;c.delete('all');self.header.delete('all');sites,times=self.slots()
        width=self.TIME_W+len(sites)*self.CELL_W;height=self.HEAD_H+(len(times)-1)*self.CELL_H
        c.configure(scrollregion=(0,0,width,height))
        self.header.configure(scrollregion=(0,0,width,36))
        self.header.create_text(12,18,anchor='w',text='时段 / 场地',fill='#334155',font=('Microsoft YaHei UI',10,'bold'))
        for col,site in enumerate(sites):
            self.header.create_text(self.TIME_W+col*self.CELL_W+self.CELL_W/2,18,text=f'{site} 号',fill='#334155',font=('Microsoft YaHei UI',10,'bold'))
        for row in range(len(times)-1):
            y=self.HEAD_H+row*self.CELL_H
            c.create_text(10,y+self.CELL_H/2,anchor='w',text=f'{times[row]}–{times[row+1]}',fill='#475569',font=('Microsoft YaHei UI',10))
            for col,site in enumerate(sites):
                slot=SelectedSlot(times[row],times[row+1],site);state=self.model.state(slot)
                bg,fg=('#2563eb','white') if state=='selected' else ('white','#64748b') if state=='available' else ('#e5e7eb','#9ca3af')
                if state=='available' and self.model.existing(slot):bg,fg='#eff6ff','#2563eb'
                x=self.TIME_W+col*self.CELL_W
                border='#0f172a' if (row,col)==self.focus_cell and c.focus_get()==c else '#d5deea'
                c.create_rectangle(x+3,y+3,x+self.CELL_W-3,y+self.CELL_H-3,fill=bg,outline=border,width=2 if border=='#0f172a' else 1)
                self.person(x+self.CELL_W/2,y+self.CELL_H/2-1,fg)
    def click(self,event):
        if not self.enabled:return
        item=self.at(event.x,event.y)
        if item:
            self.focus_cell=item[:2];self.canvas.focus_set()
            self.choose(item[2])
    def choose(self,slot):
        try:self.model.toggle(slot);self.on_change()
        except BookingError as exc:self.on_hint(str(exc))
        self.draw()
    def hover(self,event):
        item=self.at(event.x,event.y)
        if item:
            slot=item[2];state=self.model.state(slot)
            label={'available':'可选','selected':'已选，再点取消'}.get(state,state)
            if state=='available' and self.model.existing(slot):label='已有同时间本地计划，提交时需确认覆盖'
            self.canvas.configure(cursor='hand2' if state in ('available','selected') else 'arrow')
            self.on_hint(f'{slot.site_id}号场 {slot.start}–{slot.end} · {label}')
    def move(self,dr,dc):
        if self.model is None:return 'break'
        sites,times=self.slots();r,c=self.focus_cell
        self.focus_cell=(max(0,min(len(times)-2,r+dr)),max(0,min(len(sites)-1,c+dc)))
        self.draw();return 'break'
    def activate(self,event=None):
        if self.model and self.enabled:
            sites,times=self.slots();r,c=self.focus_cell
            self.choose(SelectedSlot(times[r],times[r+1],sites[c]))
        return 'break'
